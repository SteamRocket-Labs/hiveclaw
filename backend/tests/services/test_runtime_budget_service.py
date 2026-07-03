from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select


async def _seed_tenant(owner_sessionmaker, *, name: str = "Runtime Budget Tenant") -> uuid.UUID:
    from app.models.tenant import Tenant

    tenant_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=name, slug=f"rt-budget-{tenant_id.hex[:8]}"))
        await db.commit()
    return tenant_id


async def _create_run(service, tenant_id: uuid.UUID, **overrides):
    from app.services.runtime_budget_service import RuntimeBudgetRunCreate

    payload = {
        "tenant_id": tenant_id,
        "root_run_kind": "trigger_fire",
        "root_run_key": f"trigger:{uuid.uuid4()}",
        "source": "scheduled",
        "profile": "scheduled",
        "max_tokens": 10_000,
        "max_cache_miss_tokens": 2_000,
        "max_subagents": 2,
        "max_delegations": 2,
        "max_background_tasks": 2,
        "max_continuation_wakes": 2,
        "max_provider_calls": 5,
        "enforcement_mode": "enforce",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    payload.update(overrides)
    return await service.create_run(RuntimeBudgetRunCreate(**payload))


async def test_runtime_budget_service_wraps_database_sessions_in_audited_bypass(monkeypatch):
    from app.services import runtime_budget_service as module

    calls: list[str] = []

    class _Scalars:
        def all(self):
            return []

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, *_args, **_kwargs):
            return _Result()

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        calls.append(reason)
        yield session

    monkeypatch.setattr(module, "enter_rls_bypass", fake_enter_rls_bypass)

    service = module.RuntimeBudgetService(session_factory=_Session)
    await service.resolve_policy(module.RuntimeBudgetPolicyLookup(tenant_id=uuid.uuid4()))

    assert calls == ["runtime_budget_service.resolve_policy"]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_runtime_budget_service_uses_audited_bypass_for_background_sessions(
    app_user_sessionmaker,
    owner_sessionmaker,
):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService, RuntimeBudgetSettlement

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)

    run = await _create_run(service, tenant_id, max_provider_calls=2, max_tokens=1_000)
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="provider-call",
            provider_calls=1,
            tokens=500,
            reason="background provider call estimate",
        )
    )
    await service.settle(
        RuntimeBudgetSettlement(
            budget_run_id=run.id,
            reservation_key="provider-call",
            actual_provider_calls=1,
            actual_tokens=450,
            reason="background provider call settled",
        )
    )

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        events = (
            await db.execute(
                select(RuntimeBudgetEvent)
                .where(RuntimeBudgetEvent.budget_run_id == run.id)
                .order_by(RuntimeBudgetEvent.created_at)
            )
        ).scalars().all()

    assert stored.tenant_id == tenant_id
    assert stored.reserved_provider_calls == 0
    assert stored.used_provider_calls == 1
    assert stored.used_tokens == 450
    assert [event.event_type for event in events] == ["reservation", "settlement"]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_runtime_budget_service_bypass_keeps_api_reads_tenant_scoped(
    app_user_sessionmaker,
    owner_sessionmaker,
):
    from app.services.runtime_budget_service import RuntimeBudgetService

    first_tenant_id = await _seed_tenant(owner_sessionmaker, name="Budget Tenant A")
    second_tenant_id = await _seed_tenant(owner_sessionmaker, name="Budget Tenant B")
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)

    first_run = await _create_run(service, first_tenant_id, root_run_key=f"trigger:{uuid.uuid4()}")
    second_run = await _create_run(service, second_tenant_id, root_run_key=f"trigger:{uuid.uuid4()}")

    first_tenant_runs = await service.list_runs(tenant_id=first_tenant_id)
    first_visible = await service.get_run(tenant_id=first_tenant_id, budget_run_id=first_run.id)
    cross_tenant_hidden = await service.get_run(tenant_id=first_tenant_id, budget_run_id=second_run.id)

    assert [run.id for run in first_tenant_runs] == [first_run.id]
    assert first_visible is not None
    assert cross_tenant_hidden is None


@pytest.mark.usefixtures("migrated_pg_url")
async def test_policy_resolution_chooses_most_specific_enabled_policy(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetPolicy
    from app.services.runtime_budget_service import RuntimeBudgetPolicyLookup, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    agent_id = uuid.uuid4()
    trigger_id = uuid.uuid4()
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)

    async with owner_sessionmaker() as db:
        db.add_all(
            [
                RuntimeBudgetPolicy(
                    name="platform",
                    tenant_id=None,
                    scope_type="platform_default",
                    max_subagents=1,
                    priority=0,
                ),
                RuntimeBudgetPolicy(
                    name="tenant",
                    tenant_id=tenant_id,
                    scope_type="tenant_default",
                    max_subagents=2,
                    priority=10,
                ),
                RuntimeBudgetPolicy(
                    name="profile",
                    tenant_id=tenant_id,
                    scope_type="source_profile",
                    source="scheduled",
                    profile="scheduled",
                    max_subagents=3,
                    priority=20,
                ),
                RuntimeBudgetPolicy(
                    name="agent",
                    tenant_id=tenant_id,
                    scope_type="agent",
                    agent_id=agent_id,
                    max_subagents=4,
                    priority=30,
                ),
                RuntimeBudgetPolicy(
                    name="trigger-disabled",
                    tenant_id=tenant_id,
                    scope_type="trigger",
                    trigger_id=trigger_id,
                    max_subagents=99,
                    priority=40,
                    enabled=False,
                ),
                RuntimeBudgetPolicy(
                    name="agent-trigger",
                    tenant_id=tenant_id,
                    scope_type="agent_trigger",
                    agent_id=agent_id,
                    trigger_id=trigger_id,
                    max_subagents=5,
                    priority=50,
                ),
            ]
        )
        await db.commit()

    policy = await service.resolve_policy(
        RuntimeBudgetPolicyLookup(
            tenant_id=tenant_id,
            source="scheduled",
            profile="scheduled",
            agent_id=agent_id,
            trigger_id=trigger_id,
        )
    )

    assert policy.name == "agent-trigger"
    assert policy.max_subagents == 5


def test_reservation_estimate_uses_default_prompt_and_observed_floor():
    from app.services.runtime_budget_service import estimate_reservation_tokens

    assert estimate_reservation_tokens(
        default_tokens=1_000,
        prompt_tokens=2_500,
        observed_floor_tokens=84_868,
    ) == 84_868


@pytest.mark.usefixtures("migrated_pg_url")
async def test_enforce_reservation_denies_without_incrementing(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import RuntimeBudgetDenied, RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_subagents=1)
    task_id = uuid.uuid4()

    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="pending",
                budget_run_id=run.id,
            )
        )
        await db.commit()

    first = await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="child-1",
            subagents=1,
            reason="first child",
        )
    )
    assert first.allowed is True

    with pytest.raises(RuntimeBudgetDenied):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key="child-2",
                subagents=1,
                reason="second child",
            )
        )

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        task = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == task_id))).scalar_one()
        events = (
            await db.execute(
                select(RuntimeBudgetEvent).where(RuntimeBudgetEvent.budget_run_id == run.id).order_by(RuntimeBudgetEvent.created_at)
            )
        ).scalars().all()

    assert stored.reserved_subagents == 1
    assert stored.used_subagents == 0
    assert stored.status == "exhausted"
    assert task.status == "killed"
    assert task.budget_terminal_reason == "runtime_budget_exhausted"
    assert [event.event_type for event in events] == ["reservation", "denial"]
    assert events[-1].allowed is False


@pytest.mark.usefixtures("migrated_pg_url")
async def test_observe_mode_records_would_deny_but_allows(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, enforcement_mode="observe", max_subagents=1)

    result = await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="observe-child",
            subagents=2,
            reason="observe overrun",
        )
    )

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        event = (await db.execute(select(RuntimeBudgetEvent).where(RuntimeBudgetEvent.budget_run_id == run.id))).scalar_one()

    assert result.allowed is True
    assert result.would_deny is True
    assert stored.reserved_subagents == 2
    assert event.event_type == "reservation"
    assert event.would_deny is True


@pytest.mark.usefixtures("migrated_pg_url")
async def test_summary_only_mode_disables_work_amplifying_reservations(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetDenied, RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="summary_only", max_tokens=1_000, max_subagents=0)

    with pytest.raises(RuntimeBudgetDenied):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key="child-denied",
                subagents=1,
                reason="fanout denied into summary only",
            )
        )

    provider_result = await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="summary-provider-call",
            tokens=100,
            provider_calls=1,
            reason="summary only final answer",
        )
    )
    with pytest.raises(RuntimeBudgetDenied):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key="child-still-denied",
                subagents=1,
                reason="summary only blocks additional work",
            )
        )

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()

    assert provider_result.allowed is True
    assert stored.status == "summary_only"
    assert stored.reserved_provider_calls == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reservation_key_is_idempotent(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_subagents=5)
    request = RuntimeBudgetReservation(
        budget_run_id=run.id,
        reservation_key="same-child",
        subagents=2,
        reason="idempotent child",
    )

    first = await service.reserve(request)
    second = await service.reserve(request)

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()

    assert first.allowed is True
    assert second.allowed is True
    assert second.idempotent is True
    assert stored.reserved_subagents == 2


@pytest.mark.usefixtures("migrated_pg_url")
async def test_concurrent_reservations_do_not_overspend_same_run(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetDenied, RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    setup_service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(setup_service, tenant_id, max_subagents=1)

    async def reserve_child(key: str):
        service = RuntimeBudgetService(session_factory=owner_sessionmaker)
        return await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key=key,
                subagents=1,
                reason=key,
            )
        )

    results = await asyncio.gather(
        reserve_child("child-a"),
        reserve_child("child-b"),
        return_exceptions=True,
    )

    allowed = [result for result in results if not isinstance(result, Exception)]
    denied = [result for result in results if isinstance(result, RuntimeBudgetDenied)]
    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()

    assert len(allowed) == 1
    assert len(denied) == 1
    assert stored.reserved_subagents == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_settlement_releases_reserved_and_records_actual_usage(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService, RuntimeBudgetSettlement

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_tokens=1_000, max_subagents=2)

    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="settle-child",
            tokens=100,
            subagents=1,
            reason="child estimate",
        )
    )
    await service.settle(
        RuntimeBudgetSettlement(
            budget_run_id=run.id,
            reservation_key="settle-child",
            actual_tokens=80,
            actual_subagents=1,
            reason="child completed",
        )
    )

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        events = (
            await db.execute(
                select(RuntimeBudgetEvent).where(RuntimeBudgetEvent.budget_run_id == run.id).order_by(RuntimeBudgetEvent.created_at)
            )
        ).scalars().all()

    assert stored.reserved_tokens == 0
    assert stored.used_tokens == 80
    assert stored.reserved_subagents == 0
    assert stored.used_subagents == 1
    assert [event.event_type for event in events] == ["reservation", "settlement"]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reaper_expires_run_releases_reservations_and_kills_pending_runtime_tasks(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    now = datetime.now(UTC)
    run = await _create_run(service, tenant_id, expires_at=now - timedelta(seconds=1), max_subagents=5)
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="dangling-child",
            subagents=3,
            reason="will expire",
        )
    )
    task_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="pending",
                budget_run_id=run.id,
                budget_reservation_key="dangling-child",
            )
        )
        await db.commit()

    expired = await service.reap_expired_runs(now=now)

    async with owner_sessionmaker() as db:
        stored_run = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        stored_task = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == task_id))).scalar_one()

    assert expired == 1
    assert stored_run.status == "expired"
    assert stored_run.reserved_subagents == 0
    assert stored_task.status == "killed"
    assert stored_task.budget_terminal_reason == "budget_run_expired"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconcile_orphaned_reservations_releases_terminal_task_reservation(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_subagents=5)
    task_id = uuid.uuid4()
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="terminal-child",
            subagents=3,
            runtime_task_id=task_id,
            reason="child task start",
        )
    )
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="killed",
                budget_run_id=run.id,
                budget_reservation_key="terminal-child",
            )
        )
        await db.commit()

    reconciled = await service.reconcile_orphaned_reservations()

    async with owner_sessionmaker() as db:
        stored_run = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        events = (
            await db.execute(
                select(RuntimeBudgetEvent)
                .where(RuntimeBudgetEvent.budget_run_id == run.id)
                .order_by(RuntimeBudgetEvent.created_at)
            )
        ).scalars().all()

    assert reconciled == 1
    assert stored_run.reserved_subagents == 0
    assert [event.event_type for event in events] == ["reservation", "settlement"]
    assert events[-1].reason == "orphaned_reservation_reconciled"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_policy_write_approve_overrun_and_tenant_mode_switch(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetPolicy, RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    policy = await service.create_policy(
        tenant_id=tenant_id,
        name="tenant scheduled",
        scope_type="tenant_default",
        max_subagents=8,
        enforcement_mode="observe",
    )
    updated = await service.update_policy(
        tenant_id=tenant_id,
        policy_id=policy.id,
        updates={"enforcement_mode": "enforce", "max_subagents": 6},
    )
    run = await _create_run(service, tenant_id, max_subagents=1)
    approved = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        reason="reviewed",
        actor_user_id=uuid.uuid4(),
        enforcement_mode="observe",
        max_subagents=12,
    )
    switched = await service.set_tenant_enforcement_mode(
        tenant_id=tenant_id,
        enforcement_mode="observe",
        reason="emergency",
        actor_user_id=uuid.uuid4(),
    )

    async with owner_sessionmaker() as db:
        stored_policy = (await db.execute(select(RuntimeBudgetPolicy).where(RuntimeBudgetPolicy.id == policy.id))).scalar_one()
        stored_run = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        event = (
            await db.execute(
                select(RuntimeBudgetEvent).where(
                    RuntimeBudgetEvent.budget_run_id == run.id,
                    RuntimeBudgetEvent.event_type == "overrun_approved",
                )
            )
        ).scalar_one()

    assert updated is not None
    assert approved is not None
    assert switched == 1
    assert stored_policy.enforcement_mode == "observe"
    assert stored_policy.max_subagents == 6
    assert stored_run.status == "active"
    assert stored_run.enforcement_mode == "observe"
    assert stored_run.max_subagents == 12
    assert event.reason == "reviewed"


def test_budget_service_failure_modes():
    from app.services.runtime_budget_service import BudgetFailureContext, decide_budget_service_failure

    interactive = decide_budget_service_failure(
        BudgetFailureContext(source="web_chat", interactive=True, work_amplifying=False)
    )
    foreground_subagent = decide_budget_service_failure(
        BudgetFailureContext(source="web_chat", interactive=True, work_amplifying=True)
    )
    scheduled = decide_budget_service_failure(
        BudgetFailureContext(source="scheduled", interactive=False, work_amplifying=True)
    )

    assert interactive.fail_open is True
    assert interactive.disable_work_amplifying_tools is True
    assert foreground_subagent.fail_closed is True
    assert scheduled.fail_closed is True
