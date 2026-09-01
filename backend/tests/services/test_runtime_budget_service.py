from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete as sa_delete, select


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
        "max_team_sessions": 2,
        "max_delegations": 2,
        "max_background_tasks": 2,
        "max_continuation_wakes": 2,
        "max_provider_calls": 5,
        "enforcement_mode": "enforce",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    payload.update(overrides)
    return await service.create_run(RuntimeBudgetRunCreate(**payload))


def _mark_waiting_episode(run) -> uuid.UUID:
    episode_id = uuid.uuid4()
    run.status = "waiting_budget_approval"
    run.metadata_json = {**(run.metadata_json or {}), "approval_episode_id": str(episode_id)}
    return episode_id


async def _current_episode(service, tenant_id: uuid.UUID, budget_run_id: uuid.UUID) -> uuid.UUID:
    run = await service.get_run(tenant_id=tenant_id, budget_run_id=budget_run_id)
    assert run is not None and run.status == "waiting_budget_approval"
    assert run.approval_episode_id is not None
    return run.approval_episode_id


def test_orphan_reconciliation_never_infers_actuals_without_declared_terminal_intent() -> None:
    from app.services.runtime_budget_service import _orphan_reconciliation_actual

    reserved = {
        "tokens": 50_000,
        "cache_miss_tokens": 50_000,
        "subagents": 1,
        "background_tasks": 1,
    }
    failed_before_spawn = SimpleNamespace(
        task_type="subagent",
        status="failed",
        token_usage={},
        metadata_json={"worker_dispatch_failed": True},
        started_at=datetime.now(UTC),
        claimed_by="worker-a",
    )
    actual, source, unknown = _orphan_reconciliation_actual(failed_before_spawn, reserved)
    assert actual == {}
    assert source == "runtime_task_actuals_missing"
    assert unknown == ["background_tasks", "cache_miss_tokens", "subagents", "tokens"]

    killed_after_dispatch = SimpleNamespace(
        task_type="subagent",
        status="killed",
        token_usage={"total_tokens": 9, "cache_miss_tokens": 4},
        metadata_json={"worker_dispatched": True},
        started_at=datetime.now(UTC),
        claimed_by="worker-a",
    )
    actual, source, unknown = _orphan_reconciliation_actual(killed_after_dispatch, reserved)
    assert actual == {}
    assert source == "runtime_task_actuals_missing"
    assert unknown == ["background_tasks", "cache_miss_tokens", "subagents", "tokens"]


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
    from app.services.runtime_budget_service import (
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetSettlement,
    )

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
            (
                await db.execute(
                    select(RuntimeBudgetEvent)
                    .where(RuntimeBudgetEvent.budget_run_id == run.id)
                    .order_by(RuntimeBudgetEvent.created_at)
                )
            )
            .scalars()
            .all()
        )

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

    try:
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
    finally:
        # Shared PG container: leaked enabled policies would shadow the builtin
        # fallback for later tests in the same session (assert 1 == 24 class of
        # order-dependent failures). Polluter cleans up.
        async with owner_sessionmaker() as db:
            await db.execute(sa_delete(RuntimeBudgetPolicy))
            await db.commit()


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (
            "interactive",
            {
                "max_subagents": 24,
                "max_team_sessions": 4,
                "max_delegations": 16,
                "max_continuation_wakes": 64,
                "max_provider_calls": 300,
                "max_tokens": 50_000_000,
                "max_cache_miss_tokens": 10_000_000,
                "default_child_token_reservation": 200_000,
                "fail_mode": "require_confirmation",
            },
        ),
        (
            "scheduled",
            {
                "max_subagents": 32,
                "max_team_sessions": 0,
                "max_delegations": 12,
                "max_continuation_wakes": 64,
                "max_provider_calls": 240,
                "max_tokens": 40_000_000,
                "max_cache_miss_tokens": 8_000_000,
                "default_child_token_reservation": 250_000,
                "fail_mode": "summary_only",
            },
        ),
        (
            "workflow",
            {
                "max_subagents": 256,
                "max_team_sessions": 0,
                "max_delegations": 64,
                "max_continuation_wakes": 512,
                "max_provider_calls": 2_000,
                "max_tokens": 250_000_000,
                "max_cache_miss_tokens": 80_000_000,
                "default_child_token_reservation": 300_000,
                "fail_mode": "hard_stop",
            },
        ),
        (
            "agent_team",
            {
                "max_subagents": 16,
                "max_team_sessions": 4,
                "max_delegations": 16,
                "max_continuation_wakes": 96,
                "max_provider_calls": 500,
                "max_tokens": 80_000_000,
                "max_cache_miss_tokens": 16_000_000,
                "default_child_token_reservation": 250_000,
                "fail_mode": "require_confirmation",
            },
        ),
    ],
)
async def test_builtin_policy_uses_documented_profile_defaults(owner_sessionmaker, profile, expected):
    from app.services.runtime_budget_service import RuntimeBudgetPolicyLookup, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)

    policy = await service.resolve_policy(
        RuntimeBudgetPolicyLookup(
            tenant_id=tenant_id,
            source=profile,
            profile=profile,
        )
    )

    for key, value in expected.items():
        assert getattr(policy, key) == value
    assert policy.enforcement_mode == "enforce"
    assert policy.default_llm_call_token_reservation == expected["default_child_token_reservation"]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_create_run_with_builtin_fallback_policy_does_not_write_unpersisted_policy_id(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetPolicy
    from app.services.runtime_budget_service import RuntimeBudgetPolicyLookup, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    policy = await service.resolve_policy(
        RuntimeBudgetPolicyLookup(tenant_id=tenant_id, source="trigger", profile="scheduled_job")
    )

    async with owner_sessionmaker() as db:
        persisted_policy = (
            await db.execute(select(RuntimeBudgetPolicy).where(RuntimeBudgetPolicy.id == policy.id))
        ).scalar_one_or_none()
    assert persisted_policy is None

    run = await _create_run(
        service,
        tenant_id,
        source="trigger",
        profile="scheduled_job",
        policy_id=policy.id,
        policy_snapshot={
            "policy_id": str(policy.id),
            "scope_type": policy.scope_type,
            "policy_json": policy.policy_json,
        },
    )

    assert run.policy_id is None
    assert run.policy_snapshot["policy_id"] is None
    assert run.policy_snapshot["policy_json"]["source"] == "built_in_fallback"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_interactive_company_policy_matches_web_chat_lookup(owner_sessionmaker):
    from app.services.runtime_budget_service import RuntimeBudgetPolicyLookup, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    try:
        await service.create_policy(
            tenant_id=tenant_id,
            name="interactive override",
            scope_type="source_profile",
            source="interactive",
            profile="interactive",
            max_subagents=7,
        )

        policy = await service.resolve_policy(
            RuntimeBudgetPolicyLookup(tenant_id=tenant_id, source="web", profile="web_chat_turn")
        )

        assert policy.name == "interactive override"
        assert policy.max_subagents == 7
    finally:
        # Polluter cleans up: leaked policies shadow the builtin fallback for
        # later tests sharing the session-scoped PG container.
        from app.models.runtime_budget import RuntimeBudgetPolicy

        async with owner_sessionmaker() as db:
            await db.execute(sa_delete(RuntimeBudgetPolicy))
            await db.commit()


def test_reservation_estimate_uses_default_prompt_and_observed_floor():
    from app.services.runtime_budget_service import estimate_reservation_tokens

    assert (
        estimate_reservation_tokens(
            default_tokens=1_000,
            prompt_tokens=2_500,
            observed_floor_tokens=84_868,
        )
        == 84_868
    )


@pytest.mark.parametrize(
    ("profile", "max_tokens", "max_cache_miss_tokens", "max_team_sessions", "fail_mode"),
    [
        ("interactive", 50_000_000, 10_000_000, 4, "require_confirmation"),
        ("scheduled", 40_000_000, 8_000_000, 0, "summary_only"),
        ("workflow", 250_000_000, 80_000_000, 0, "hard_stop"),
        ("agent_team", 80_000_000, 16_000_000, 4, "require_confirmation"),
    ],
)
def test_builtin_policy_unit_defaults_match_documented_profiles(
    profile,
    max_tokens,
    max_cache_miss_tokens,
    max_team_sessions,
    fail_mode,
):
    from app.services import runtime_budget_service as module

    policy = module._builtin_policy(
        module.RuntimeBudgetPolicyLookup(tenant_id=uuid.uuid4(), source=profile, profile=profile)
    )

    assert policy.max_tokens == max_tokens
    assert policy.max_cache_miss_tokens == max_cache_miss_tokens
    assert policy.max_team_sessions == max_team_sessions
    assert policy.fail_mode == fail_mode


def test_builtin_policy_maps_web_chat_profile_to_interactive_defaults():
    from app.services import runtime_budget_service as module

    policy = module._builtin_policy(
        module.RuntimeBudgetPolicyLookup(tenant_id=uuid.uuid4(), source="web", profile="web_chat_turn")
    )

    assert policy.max_subagents == 24
    assert policy.max_team_sessions == 4
    assert policy.max_cache_miss_tokens == 10_000_000
    assert policy.fail_mode == "require_confirmation"
    assert policy.policy_json == {
        "source": "built_in_fallback",
        "profile": "interactive",
        "requested_profile": "web_chat_turn",
    }


def test_builtin_fallback_policy_reference_is_not_written_as_foreign_key():
    from app.services import runtime_budget_service as module

    policy_id = uuid.uuid4()
    effective_policy_id, normalized_snapshot = module._normalize_run_policy_reference(
        policy_id,
        {
            "policy_id": str(policy_id),
            "scope_type": "tenant_default",
            "policy_json": {"source": "built_in_fallback", "profile": "scheduled"},
        },
    )

    assert effective_policy_id is None
    assert normalized_snapshot["policy_id"] is None


def test_persisted_policy_reference_is_kept_as_foreign_key():
    from app.services import runtime_budget_service as module

    policy_id = uuid.uuid4()
    effective_policy_id, normalized_snapshot = module._normalize_run_policy_reference(
        policy_id,
        {
            "policy_id": str(policy_id),
            "scope_type": "tenant_default",
            "policy_json": {"source": "company_override", "profile": "scheduled"},
        },
    )

    assert effective_policy_id == policy_id
    assert normalized_snapshot["policy_id"] == str(policy_id)


def test_interactive_source_profile_policy_matches_web_chat_lookup_unit():
    from app.models.runtime_budget import RuntimeBudgetPolicy
    from app.services import runtime_budget_service as module

    tenant_id = uuid.uuid4()
    policy = RuntimeBudgetPolicy(
        tenant_id=tenant_id,
        scope_type="source_profile",
        source="interactive",
        profile="interactive",
    )
    lookup = module.RuntimeBudgetPolicyLookup(tenant_id=tenant_id, source="web", profile="web_chat_turn")

    assert module._policy_matches(policy, lookup) is True


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
            (
                await db.execute(
                    select(RuntimeBudgetEvent)
                    .where(RuntimeBudgetEvent.budget_run_id == run.id)
                    .order_by(RuntimeBudgetEvent.created_at)
                )
            )
            .scalars()
            .all()
        )

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
        event = (
            await db.execute(select(RuntimeBudgetEvent).where(RuntimeBudgetEvent.budget_run_id == run.id))
        ).scalar_one()

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
async def test_agent_team_sessions_are_reserved_separately_from_subagents(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetDenied, RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_subagents=0, max_team_sessions=1)

    first = await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="team-member-1",
            team_sessions=1,
            reason="first teammate session",
        )
    )
    with pytest.raises(RuntimeBudgetDenied):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key="team-member-2",
                team_sessions=1,
                reason="second teammate session",
            )
        )

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()

    assert first.allowed is True
    assert stored.reserved_team_sessions == 1
    assert stored.reserved_subagents == 0
    assert stored.status == "exhausted"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reservation_key_is_idempotent(owner_sessionmaker):
    from sqlalchemy import func

    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import (
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetSettlementConflict,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_subagents=5)
    task_id = uuid.uuid4()
    request = RuntimeBudgetReservation(
        budget_run_id=run.id,
        reservation_key="same-child",
        subagents=2,
        background_tasks=1,
        runtime_task_id=task_id,
        reason="idempotent child",
    )

    first = await service.reserve(request)
    second = await service.reserve(request)
    with pytest.raises(RuntimeBudgetSettlementConflict, match="reservation runtime task conflict"):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key="same-child",
                subagents=2,
                background_tasks=1,
                runtime_task_id=uuid.uuid4(),
            )
        )
    with pytest.raises(RuntimeBudgetSettlementConflict, match="reservation amounts conflict"):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key="same-child",
                subagents=1,
                background_tasks=1,
                runtime_task_id=task_id,
            )
        )

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        reservation_count = await db.scalar(
            select(func.count())
            .select_from(RuntimeBudgetEvent)
            .where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == "same-child",
                RuntimeBudgetEvent.event_type == "reservation",
            )
        )

    assert first.allowed is True
    assert second.allowed is True
    assert second.idempotent is True
    assert stored.reserved_subagents == 2
    assert stored.reserved_background_tasks == 1
    assert reservation_count == 1


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
    from app.services.runtime_budget_service import (
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetSettlement,
    )

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
            (
                await db.execute(
                    select(RuntimeBudgetEvent)
                    .where(RuntimeBudgetEvent.budget_run_id == run.id)
                    .order_by(RuntimeBudgetEvent.created_at)
                )
            )
            .scalars()
            .all()
        )

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
    assert stored_task.metadata_json["terminal_execution_fence_ref"].startswith("runtime-task-terminal:")


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reaper_enqueues_required_terminal_boundary_in_the_same_task_settlement(owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services.runtime_budget_service import RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    now = datetime.now(UTC)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, expires_at=now - timedelta(seconds=1))
    async with owner_sessionmaker() as db:
        db.add(
            User(
                id=user_id,
                username=f"budget-{user_id.hex[:10]}",
                email=f"{user_id.hex[:12]}@budget.test",
                password_hash="x",
                display_name="Budget Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Budget Agent",
                role_description="budget terminal boundary test",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="trigger",
                status="pending",
                parent_agent_id=agent_id,
                budget_run_id=run.id,
            )
        )
        await db.commit()

    assert await service.reap_expired_runs(now=now) == 1

    async with owner_sessionmaker() as db:
        stored_task = await db.scalar(select(RuntimeTask).where(RuntimeTask.id == task_id))
        boundary = await db.scalar(
            select(RuntimeTerminalBoundaryOutbox).where(
                RuntimeTerminalBoundaryOutbox.runtime_task_id == task_id,
                RuntimeTerminalBoundaryOutbox.tenant_id == tenant_id,
            )
        )

    assert stored_task is not None
    assert stored_task.status == "killed"
    assert stored_task.terminal_boundary_enqueued_at is not None
    assert boundary is not None
    assert boundary.status == "pending"
    assert boundary.terminal_status == "killed"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reaper_expires_waiting_budget_approval_run(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    now = datetime.now(UTC)
    run = await _create_run(service, tenant_id, expires_at=now - timedelta(seconds=1))
    task_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        stored_run = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        stored_run.status = "waiting_budget_approval"
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="workflow",
                status="pending",
                budget_run_id=run.id,
                budget_admission_status="waiting_budget_approval",
            )
        )
        await db.commit()

    expired = await service.reap_expired_runs(now=now)

    async with owner_sessionmaker() as db:
        stored_run = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        stored_task = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == task_id))).scalar_one()

    assert expired == 1
    assert stored_run.status == "expired"
    assert stored_task.status == "killed"
    assert stored_task.budget_admission_status == "cancelled"


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
            (
                await db.execute(
                    select(RuntimeBudgetEvent)
                    .where(RuntimeBudgetEvent.budget_run_id == run.id)
                    .order_by(RuntimeBudgetEvent.created_at)
                )
            )
            .scalars()
            .all()
        )

    assert reconciled == 1
    assert stored_run.reserved_subagents == 0
    assert [event.event_type for event in events] == ["reservation", "settlement"]
    assert events[-1].reason == "orphaned_reservation_reconciled"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconcile_orphaned_completed_subagent_uses_terminal_actuals(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(
        service,
        tenant_id,
        max_tokens=100_000,
        max_cache_miss_tokens=100_000,
        max_subagents=2,
        max_background_tasks=2,
    )
    task_id = uuid.uuid4()
    reservation_key = f"subagent:{task_id.hex}:start"
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key=reservation_key,
            tokens=50_000,
            cache_miss_tokens=50_000,
            subagents=1,
            background_tasks=1,
            runtime_task_id=task_id,
        )
    )
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                token_usage={"total_tokens": 40},
                metadata_json={
                    "runtime_budget_actuals": {
                        "tokens": 40,
                        "cache_miss_tokens": 40,
                        "subagents": 1,
                        "background_tasks": 1,
                    }
                },
                budget_run_id=run.id,
                budget_reservation_key=reservation_key,
                budget_admission_status="reserved",
            )
        )
        await db.commit()

    assert await service.reconcile_orphaned_reservations() == 1

    async with owner_sessionmaker() as db:
        stored_run = await db.get(RuntimeBudgetRun, run.id)
        stored_task = await db.get(RuntimeTask, task_id)
        settlement = (
            await db.execute(
                select(RuntimeBudgetEvent).where(
                    RuntimeBudgetEvent.budget_run_id == run.id,
                    RuntimeBudgetEvent.reservation_key == reservation_key,
                    RuntimeBudgetEvent.event_type == "settlement",
                )
            )
        ).scalar_one()

    assert stored_run is not None and stored_task is not None
    assert stored_run.reserved_tokens == 0
    assert stored_run.reserved_cache_miss_tokens == 0
    assert stored_run.reserved_subagents == 0
    assert stored_run.reserved_background_tasks == 0
    assert stored_run.used_tokens == 40
    assert stored_run.used_cache_miss_tokens == 40
    assert stored_run.used_subagents == 1
    assert stored_run.used_background_tasks == 1
    assert stored_task.budget_admission_status == "settled"
    assert settlement.amounts_json == {
        "tokens": 40,
        "cache_miss_tokens": 40,
        "subagents": 1,
        "background_tasks": 1,
    }
    assert settlement.metadata_json["actual_source"] == "runtime_task_declared_actuals"
    assert settlement.metadata_json["released_without_actual_dimensions"] == []


@pytest.mark.usefixtures("migrated_pg_url")
async def test_budget_reconciliation_skips_settled_history_and_accounts_terminal_trigger(owner_sessionmaker):
    from sqlalchemy import func

    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import (
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetSettlement,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_background_tasks=10)
    for index in range(2):
        key = f"settled-history-{index}"
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key=key,
                background_tasks=1,
            )
        )
        await service.settle(
            RuntimeBudgetSettlement(
                budget_run_id=run.id,
                reservation_key=key,
            )
        )

    task_id = uuid.uuid4()
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="new-terminal-trigger",
            background_tasks=1,
            runtime_task_id=task_id,
        )
    )
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="trigger",
                status="skipped",
                metadata_json={"runtime_budget_actuals": {"background_tasks": 1}},
                budget_run_id=run.id,
                budget_reservation_key="new-terminal-trigger",
                budget_admission_status="reserved",
            )
        )
        await db.commit()

    assert await service.reconcile_orphaned_reservations(limit=2) >= 1

    async with owner_sessionmaker() as db:
        stored_run = await db.get(RuntimeBudgetRun, run.id)
        stored_task = await db.get(RuntimeTask, task_id)
        settlements = await db.scalar(
            select(func.count())
            .select_from(RuntimeBudgetEvent)
            .where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == "new-terminal-trigger",
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )
        settlement = await db.scalar(
            select(RuntimeBudgetEvent).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == "new-terminal-trigger",
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )

    assert stored_run is not None and stored_task is not None and settlement is not None
    assert stored_run.reserved_background_tasks == 0
    assert stored_run.used_background_tasks == 1
    assert stored_task.budget_admission_status == "settled"
    assert settlements == 1
    assert settlement.amounts_json == {"background_tasks": 1}


@pytest.mark.usefixtures("migrated_pg_url")
async def test_budget_reconciliation_repairs_marker_after_settlement_split_commit(owner_sessionmaker):
    from sqlalchemy import func

    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import (
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetSettlement,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_background_tasks=10)
    task_id = uuid.uuid4()
    reservation_key = "split-terminal-trigger"
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key=reservation_key,
            background_tasks=1,
            runtime_task_id=task_id,
        )
    )
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="trigger",
                status="completed",
                budget_run_id=run.id,
                budget_reservation_key=reservation_key,
                budget_admission_status="reserved",
            )
        )
        await db.commit()
    await service.settle(
        RuntimeBudgetSettlement(
            budget_run_id=run.id,
            reservation_key=reservation_key,
            actual_background_tasks=1,
            runtime_task_id=task_id,
        )
    )

    assert await service.reconcile_orphaned_reservations() == 1

    async with owner_sessionmaker() as db:
        stored_run = await db.get(RuntimeBudgetRun, run.id)
        stored_task = await db.get(RuntimeTask, task_id)
        settlements = await db.scalar(
            select(func.count())
            .select_from(RuntimeBudgetEvent)
            .where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == reservation_key,
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )

    assert stored_run is not None and stored_task is not None
    assert stored_run.reserved_background_tasks == 0
    assert stored_run.used_background_tasks == 1
    assert stored_task.budget_admission_status == "settled"
    assert settlements == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_missing_runtime_task_reservation_observes_creation_grace_then_releases_zero(
    owner_sessionmaker,
):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_background_tasks=4)
    task_id = uuid.uuid4()
    reservation_key = f"trigger:{task_id}:start"
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key=reservation_key,
            background_tasks=1,
            runtime_task_id=task_id,
        )
    )
    async with owner_sessionmaker() as db:
        reservation = await db.scalar(
            select(RuntimeBudgetEvent).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == reservation_key,
                RuntimeBudgetEvent.event_type == "reservation",
            )
        )
    assert reservation is not None

    await service.reconcile_orphaned_reservations(
        now=reservation.created_at + timedelta(minutes=5) - timedelta(microseconds=1)
    )
    async with owner_sessionmaker() as db:
        fresh_run = await db.get(RuntimeBudgetRun, run.id)
        fresh_settlement = await db.scalar(
            select(RuntimeBudgetEvent.id).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == reservation_key,
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )
    assert fresh_run is not None
    assert fresh_run.reserved_background_tasks == 1
    assert fresh_settlement is None

    assert (
        await service.reconcile_orphaned_reservations(
            now=reservation.created_at + timedelta(minutes=5) + timedelta(microseconds=1)
        )
        >= 1
    )
    async with owner_sessionmaker() as db:
        stale_run = await db.get(RuntimeBudgetRun, run.id)
        settlement = await db.scalar(
            select(RuntimeBudgetEvent).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == reservation_key,
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )
    assert stale_run is not None and settlement is not None
    assert stale_run.reserved_background_tasks == 0
    assert stale_run.used_background_tasks == 0
    assert settlement.amounts_json == {}
    assert settlement.metadata_json["actual_source"] == "runtime_task_missing"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_unbound_reservation_uses_explicit_long_grace_then_releases_zero(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_continuation_wakes=4)
    reservation_key = f"runtime_result_page:{uuid.uuid4()}:continuation"
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key=reservation_key,
            continuation_wakes=1,
            reason="unbound outbox continuation",
        )
    )
    async with owner_sessionmaker() as db:
        reservation = await db.scalar(
            select(RuntimeBudgetEvent).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == reservation_key,
                RuntimeBudgetEvent.event_type == "reservation",
            )
        )
    assert reservation is not None and reservation.runtime_task_id is None

    await service.reconcile_orphaned_reservations(
        now=reservation.created_at + timedelta(hours=24) - timedelta(microseconds=1)
    )
    async with owner_sessionmaker() as db:
        fresh_run = await db.get(RuntimeBudgetRun, run.id)
        early_settlement = await db.scalar(
            select(RuntimeBudgetEvent.id).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == reservation_key,
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )
    assert fresh_run is not None and fresh_run.reserved_continuation_wakes == 1
    assert early_settlement is None

    assert (
        await service.reconcile_orphaned_reservations(
            now=reservation.created_at + timedelta(hours=24) + timedelta(microseconds=1)
        )
        >= 1
    )
    async with owner_sessionmaker() as db:
        stale_run = await db.get(RuntimeBudgetRun, run.id)
        settlement = await db.scalar(
            select(RuntimeBudgetEvent).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == reservation_key,
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )
    assert stale_run is not None and settlement is not None
    assert stale_run.reserved_continuation_wakes == 0
    assert stale_run.used_continuation_wakes == 0
    assert settlement.amounts_json == {}
    assert settlement.metadata_json["actual_source"] == "unbound_reservation_grace_expired"
    assert settlement.metadata_json["reservation_binding"] == "unbound"
    assert settlement.metadata_json["grace_seconds"] == 24 * 60 * 60


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reserved_task_handoff_rechecks_active_run_after_waiting_for_run_lock(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import (
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetSettlementConflict,
        assert_runtime_task_budget_reservation_open,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_background_tasks=2)
    task_id = uuid.uuid4()
    reservation_key = f"trigger:{task_id}:start"
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key=reservation_key,
            background_tasks=1,
            runtime_task_id=task_id,
        )
    )

    async with owner_sessionmaker() as terminal_db:
        locked_run = await terminal_db.scalar(
            select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id).with_for_update()
        )
        assert locked_run is not None
        locked_run.status = "cancelled"
        locked_run.terminal_reason = "concurrent cancel won"
        locked_run.completed_at = datetime.now(UTC)
        await terminal_db.flush()

        async def assert_handoff_open():
            async with owner_sessionmaker() as creator_db:
                return await assert_runtime_task_budget_reservation_open(
                    creator_db,
                    budget_run_id=run.id,
                    reservation_key=reservation_key,
                    runtime_task_id=task_id,
                )

        creator = asyncio.create_task(assert_handoff_open())
        await asyncio.sleep(0.05)
        assert not creator.done()
        await terminal_db.commit()
        with pytest.raises(RuntimeBudgetSettlementConflict, match="is cancelled"):
            await asyncio.wait_for(creator, timeout=2)

    async with owner_sessionmaker() as db:
        assert await db.get(RuntimeTask, task_id) is None


@pytest.mark.usefixtures("migrated_pg_url")
async def test_fresh_nested_provider_reservation_on_terminal_task_is_not_reconciled_as_outer(
    owner_sessionmaker,
):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import (
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetSettlement,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(
        service,
        tenant_id,
        max_tokens=100,
        max_cache_miss_tokens=100,
        max_provider_calls=2,
    )
    task_id = uuid.uuid4()
    provider_key = f"provider_call:{task_id}:fresh"
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key=provider_key,
            tokens=50,
            cache_miss_tokens=50,
            provider_calls=1,
            runtime_task_id=task_id,
        )
    )
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                completed_at=datetime.now(UTC),
                budget_run_id=run.id,
                budget_reservation_key=f"subagent:{task_id}:start",
                budget_admission_status="reserved",
            )
        )
        await db.commit()

    await service.reconcile_orphaned_reservations()
    async with owner_sessionmaker() as db:
        still_reserved = await db.get(RuntimeBudgetRun, run.id)
        early_settlement = await db.scalar(
            select(RuntimeBudgetEvent.id).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == provider_key,
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )
    assert still_reserved is not None
    assert still_reserved.reserved_provider_calls == 1
    assert still_reserved.reserved_tokens == 50
    assert early_settlement is None

    await service.settle(
        RuntimeBudgetSettlement(
            budget_run_id=run.id,
            reservation_key=provider_key,
            actual_tokens=7,
            actual_cache_miss_tokens=3,
            actual_provider_calls=1,
            runtime_task_id=task_id,
        )
    )
    async with owner_sessionmaker() as db:
        settled_run = await db.get(RuntimeBudgetRun, run.id)
        settlement = await db.scalar(
            select(RuntimeBudgetEvent).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == provider_key,
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )
    assert settled_run is not None and settlement is not None
    assert settled_run.reserved_provider_calls == 0
    assert settled_run.used_provider_calls == 1
    assert settled_run.used_tokens == 7
    assert settled_run.used_cache_miss_tokens == 3
    assert {key: value for key, value in settlement.amounts_json.items() if value} == {
        "tokens": 7,
        "cache_miss_tokens": 3,
        "provider_calls": 1,
    }


@pytest.mark.usefixtures("migrated_pg_url")
async def test_nested_orphan_grace_starts_at_task_terminal_time_not_old_reservation_time(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_tokens=100, max_provider_calls=2)
    task_id = uuid.uuid4()
    provider_key = f"provider_call:{task_id}:old"
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key=provider_key,
            tokens=50,
            provider_calls=1,
            runtime_task_id=task_id,
        )
    )
    terminal_at = datetime.now(UTC)
    async with owner_sessionmaker() as db:
        reservation = await db.scalar(
            select(RuntimeBudgetEvent).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == provider_key,
                RuntimeBudgetEvent.event_type == "reservation",
            )
        )
        assert reservation is not None
        reservation.created_at = terminal_at - timedelta(hours=2)
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                completed_at=terminal_at,
                budget_run_id=run.id,
                budget_reservation_key=f"subagent:{task_id}:start",
                budget_admission_status="reserved",
            )
        )
        await db.commit()

    await service.reconcile_orphaned_reservations(now=terminal_at + timedelta(hours=1) - timedelta(microseconds=1))
    async with owner_sessionmaker() as db:
        protected_run = await db.get(RuntimeBudgetRun, run.id)
        early_settlement = await db.scalar(
            select(RuntimeBudgetEvent.id).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == provider_key,
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )
    assert protected_run is not None and protected_run.reserved_provider_calls == 1
    assert early_settlement is None

    assert (
        await service.reconcile_orphaned_reservations(now=terminal_at + timedelta(hours=1) + timedelta(microseconds=1))
        >= 1
    )
    async with owner_sessionmaker() as db:
        settled_run = await db.get(RuntimeBudgetRun, run.id)
        settlement = await db.scalar(
            select(RuntimeBudgetEvent).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == provider_key,
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )
    assert settled_run is not None and settlement is not None
    assert settled_run.reserved_provider_calls == 0
    assert settled_run.used_provider_calls == 0
    assert settlement.metadata_json["actual_source"] == "nested_reservation_missing_settlement"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_cancel_pending_team_member_stamps_and_reconciles_dynamic_reservation_actuals(
    owner_sessionmaker,
    monkeypatch,
):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService
    from app.services.runtime_terminal_settlement import settle_runtime_task_terminal

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(
        service,
        tenant_id,
        max_team_sessions=4,
        max_background_tasks=4,
        max_continuation_wakes=4,
    )
    task_id = uuid.uuid4()
    reservation_key = f"team-member:{task_id}:start"
    expected_actuals = {
        "team_sessions": 1,
        "background_tasks": 1,
        "continuation_wakes": 1,
    }
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key=reservation_key,
            team_sessions=1,
            background_tasks=1,
            continuation_wakes=1,
            runtime_task_id=task_id,
        )
    )
    async with owner_sessionmaker() as db:
        task = RuntimeTask(
            id=task_id,
            tenant_id=tenant_id,
            task_type="team_member",
            status="pending",
            budget_run_id=run.id,
            budget_reservation_key=reservation_key,
            budget_admission_status="reserved",
            metadata_json={},
        )
        db.add(task)
        await db.commit()

    async def settle_budget_terminal_only(db, task, **kwargs):
        # The executable-chat transcript/outbox boundary has separate authority
        # fixtures; retain the shared terminal stamp while isolating this budget test.
        return await settle_runtime_task_terminal(
            db,
            task,
            terminal_source=kwargs["terminal_source"],
            root_reason_code=kwargs.get("root_reason_code"),
            root_state=kwargs.get("root_state"),
            settle_root=kwargs.get("settle_root", True),
        )

    monkeypatch.setattr(
        "app.services.runtime_terminal_settlement.settle_and_enqueue_runtime_task_terminal",
        settle_budget_terminal_only,
    )

    cancelled = await service.cancel_run(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        reason="owner cancelled queued team member",
    )
    assert cancelled is not None and cancelled.status == "cancelled"
    async with owner_sessionmaker() as db:
        killed_task = await db.get(RuntimeTask, task_id)
    assert killed_task is not None
    assert killed_task.status == "killed"
    assert killed_task.metadata_json["runtime_budget_actuals"] == expected_actuals

    assert await service.reconcile_orphaned_reservations() == 1
    assert await service.reconcile_orphaned_reservations() == 0
    async with owner_sessionmaker() as db:
        settled_run = await db.get(RuntimeBudgetRun, run.id)
        settled_task = await db.get(RuntimeTask, task_id)
        settlement = await db.scalar(
            select(RuntimeBudgetEvent).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == reservation_key,
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )
    assert settled_run is not None and settled_task is not None and settlement is not None
    assert settled_run.used_team_sessions == 1
    assert settled_run.used_background_tasks == 1
    assert settled_run.used_continuation_wakes == 1
    assert settled_task.budget_admission_status == "settled"
    assert settlement.amounts_json == expected_actuals


@pytest.mark.usefixtures("migrated_pg_url")
async def test_team_member_exact_cap_remains_claimable_until_terminal_dynamic_settlement(
    owner_sessionmaker,
):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService
    from app.services.runtime_task_claim_service import build_runtime_task_claim_statement
    from app.services.runtime_terminal_settlement import settle_runtime_task_terminal

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(
        service,
        tenant_id,
        max_team_sessions=1,
        max_background_tasks=1,
        max_continuation_wakes=1,
    )
    task_id = uuid.uuid4()
    reservation_key = f"team-member:{task_id}:start"
    expected_actuals = {
        "team_sessions": 1,
        "background_tasks": 1,
        "continuation_wakes": 1,
    }
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key=reservation_key,
            team_sessions=1,
            background_tasks=1,
            continuation_wakes=1,
            runtime_task_id=task_id,
        )
    )
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="team_member",
                status="pending",
                budget_run_id=run.id,
                budget_reservation_key=reservation_key,
                budget_admission_status="reserved",
                metadata_json={"runtime_budget_actuals": expected_actuals},
            )
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        active_run = await db.get(RuntimeBudgetRun, run.id)
        events = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent)
                    .where(RuntimeBudgetEvent.budget_run_id == run.id)
                    .order_by(RuntimeBudgetEvent.created_at)
                )
            )
            .scalars()
            .all()
        )
        claimable = list(
            (await db.execute(build_runtime_task_claim_statement(task_types=("team_member",), batch_size=100)))
            .scalars()
            .all()
        )
    assert active_run is not None
    assert active_run.status == "active"
    assert active_run.used_team_sessions == 0
    assert active_run.used_background_tasks == 0
    assert active_run.used_continuation_wakes == 0
    assert [event.event_type for event in events] == ["reservation"]
    assert task_id in {task.id for task in claimable}

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert task is not None
        task.status = "completed"
        task.completed_at = datetime.now(UTC)
        await settle_runtime_task_terminal(
            db,
            task,
            terminal_source="test:team-member-terminal",
        )
        await db.commit()

    assert await service.reconcile_orphaned_reservations() == 1
    assert await service.reconcile_orphaned_reservations() == 0
    async with owner_sessionmaker() as db:
        terminal_run = await db.get(RuntimeBudgetRun, run.id)
        terminal_task = await db.get(RuntimeTask, task_id)
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == run.id,
                        RuntimeBudgetEvent.reservation_key == reservation_key,
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert terminal_run is not None and terminal_task is not None
    assert terminal_run.status == "hard_stopped"
    assert terminal_run.reserved_team_sessions == 0
    assert terminal_run.reserved_background_tasks == 0
    assert terminal_run.reserved_continuation_wakes == 0
    assert terminal_run.used_team_sessions == 1
    assert terminal_run.used_background_tasks == 1
    assert terminal_run.used_continuation_wakes == 1
    assert terminal_task.budget_admission_status == "settled"
    assert len(settlements) == 1
    assert settlements[0].amounts_json == expected_actuals


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
    async with owner_sessionmaker() as db:
        waiting_run = await db.get(RuntimeBudgetRun, run.id)
        assert waiting_run is not None
        approval_episode_id = _mark_waiting_episode(waiting_run)
        await db.commit()
    approved = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        reason="reviewed",
        approval_episode_id=approval_episode_id,
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
        stored_policy = (
            await db.execute(select(RuntimeBudgetPolicy).where(RuntimeBudgetPolicy.id == policy.id))
        ).scalar_one()
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


@pytest.mark.usefixtures("migrated_pg_url")
async def test_approve_overrun_cannot_revive_concurrently_cancelled_run(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetService, RuntimeBudgetStateConflict

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_subagents=1)
    async with owner_sessionmaker() as db:
        waiting_run = await db.get(RuntimeBudgetRun, run.id)
        assert waiting_run is not None
        approval_episode_id = _mark_waiting_episode(waiting_run)
        await db.commit()

    async with owner_sessionmaker() as cancel_db:
        cancelled_run = await cancel_db.scalar(
            select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id).with_for_update()
        )
        assert cancelled_run is not None
        cancelled_run.status = "cancelled"
        cancelled_run.terminal_reason = "owner cancelled while approval was stale"
        cancelled_run.completed_at = datetime.now(UTC)
        await cancel_db.flush()

        approval = asyncio.create_task(
            service.approve_overrun(
                tenant_id=tenant_id,
                budget_run_id=run.id,
                reason="stale approval",
                approval_episode_id=approval_episode_id,
                max_subagents=2,
            )
        )
        await asyncio.sleep(0.05)
        assert not approval.done()
        await cancel_db.commit()
        with pytest.raises(RuntimeBudgetStateConflict, match="is cancelled"):
            await asyncio.wait_for(approval, timeout=2)

    async with owner_sessionmaker() as db:
        stored_run = await db.get(RuntimeBudgetRun, run.id)
        approval_event = await db.scalar(
            select(RuntimeBudgetEvent.id).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.event_type == "overrun_approved",
            )
        )
    assert stored_run is not None and stored_run.status == "cancelled"
    assert stored_run.max_subagents == 1
    assert approval_event is None


@pytest.mark.usefixtures("migrated_pg_url")
async def test_require_confirmation_freezes_and_approval_resumes_exact_pending_task(
    owner_sessionmaker,
    monkeypatch,
):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_root_item import RuntimeRootItem
    from app.models.runtime_task import RuntimeTask
    from app.services import runtime_task_worker
    from app.services.runtime_budget_service import (
        RuntimeBudgetApprovalRequired,
        RuntimeBudgetReservation,
        RuntimeBudgetService,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(
        service,
        tenant_id,
        fail_mode="require_confirmation",
        max_subagents=0,
        max_background_tasks=0,
    )
    task_id = uuid.uuid4()
    source_agent_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="pending",
                budget_run_id=run.id,
                budget_reservation_key="approval-child",
                budget_admission_status="admitting",
                root_runtime_task_id=task_id,
            )
        )
        await db.flush()
        db.add(
            RuntimeRootItem(
                tenant_id=tenant_id,
                root_runtime_task_id=task_id,
                runtime_task_id=task_id,
                source_agent_id=source_agent_id,
                intent_key=f"subagent:{task_id}",
                work_type="subagent",
                target_ref="subagent:reviewer",
                path_json=[f"agent:{source_agent_id}", "subagent:reviewer"],
                state="waiting_approval",
                admission_disposition="deferred",
                approval_ref=f"runtime-budget://{run.id}/reservation/approval-child",
            )
        )
        await db.commit()

    with pytest.raises(RuntimeBudgetApprovalRequired):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key="approval-child",
                subagents=1,
                background_tasks=1,
                runtime_task_id=task_id,
                reason="spawn child",
            )
        )

    async with owner_sessionmaker() as db:
        frozen_run = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        frozen_task = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == task_id))).scalar_one()
    assert frozen_run.status == "waiting_budget_approval"
    assert frozen_run.completed_at is None
    assert frozen_task.status == "pending"
    assert frozen_task.budget_admission_status == "waiting_budget_approval"

    wakeups: list[str] = []

    async def fake_notify_runtime_task_worker(*, reason, runtime_task_id=None):
        wakeups.append(f"{reason}:{runtime_task_id}")

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", fake_notify_runtime_task_worker)
    actor_id = uuid.uuid4()
    approval_episode_id = await _current_episode(service, tenant_id, run.id)
    approved = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        reason="owner approved one child",
        approval_episode_id=approval_episode_id,
        actor_user_id=actor_id,
    )
    replayed = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        reason="owner approved one child",
        approval_episode_id=approval_episode_id,
        actor_user_id=actor_id,
    )

    async with owner_sessionmaker() as db:
        resumed_task = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == task_id))).scalar_one()
        resumed_root_item = (
            await db.execute(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == task_id))
        ).scalar_one()
        events = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent)
                    .where(RuntimeBudgetEvent.budget_run_id == run.id)
                    .order_by(RuntimeBudgetEvent.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert approved is not None
    assert replayed is not None and replayed.id == approved.id
    assert approved.status == "active"
    assert approved.enforcement_mode == "enforce"
    assert approved.reserved_subagents == 1
    assert approved.reserved_background_tasks == 1
    assert approved.max_subagents == 1
    assert approved.max_background_tasks == 1
    assert resumed_task.status == "pending"
    assert resumed_task.budget_admission_status == "approved"
    assert resumed_task.budget_terminal_reason is None
    assert resumed_root_item.state == "queued"
    assert resumed_root_item.admission_disposition == "admitted"
    assert resumed_root_item.reason_code == "runtime_budget_approval_granted"
    assert resumed_root_item.approval_ref == f"runtime-budget-event://{events[-1].id}"
    assert [event.event_type for event in events] == ["denial", "reservation", "overrun_approved"]
    assert events[-1].metadata_json["actor_user_id"] == str(actor_id)
    assert events[-1].metadata_json["resumed_runtime_task_ids"] == [str(task_id)]
    assert wakeups == [f"runtime_budget_approved:{task_id}"]


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.parametrize(
    ("task_type", "admission_marker"),
    [("heartbeat", "root"), ("web_chat_turn", "inherited")],
)
async def test_breaker_approval_restores_no_reservation_root_marker_and_claimability(
    owner_sessionmaker,
    monkeypatch,
    task_type,
    admission_marker,
):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import RuntimeBudgetService
    from app.services.runtime_task_claim_service import build_runtime_task_claim_statement

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(
        service,
        tenant_id,
        fail_mode="require_confirmation",
        max_parent_invocations=1,
    )
    task_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type=task_type,
                status="pending",
                budget_run_id=run.id,
                budget_reservation_key=None,
                budget_admission_status=admission_marker,
                metadata_json={"marker_before_breaker": admission_marker},
            )
        )
        await db.commit()

    reason = await service.evaluate_wake_breaker(
        tenant_id=tenant_id,
        budget_run_id=run.id,
    )
    assert reason is not None and "parent_invocations" in reason
    async with owner_sessionmaker() as db:
        frozen_run = await db.get(RuntimeBudgetRun, run.id)
        frozen_task = await db.get(RuntimeTask, task_id)
    assert frozen_run is not None and frozen_task is not None
    assert frozen_run.status == "waiting_budget_approval"
    assert frozen_task.budget_admission_status == "waiting_budget_approval"
    assert frozen_task.metadata_json["budget_admission_status_before_breaker"] == admission_marker

    wakeups: list[uuid.UUID | None] = []

    async def fake_notify_runtime_task_worker(*, reason, runtime_task_id=None):
        assert reason == "runtime_budget_approved"
        wakeups.append(runtime_task_id)

    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        fake_notify_runtime_task_worker,
    )
    approval_episode_id = await _current_episode(service, tenant_id, run.id)
    approved = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        reason="owner resumed root work",
        approval_episode_id=approval_episode_id,
    )
    async with owner_sessionmaker() as db:
        resumed_task = await db.get(RuntimeTask, task_id)
        claimable = list(
            (await db.execute(build_runtime_task_claim_statement(task_types=(task_type,), batch_size=100)))
            .scalars()
            .all()
        )
    assert approved is not None and approved.status == "active"
    assert resumed_task is not None
    assert resumed_task.status == "pending"
    assert resumed_task.budget_admission_status == admission_marker
    assert resumed_task.budget_terminal_reason is None
    assert "budget_admission_status_before_breaker" not in resumed_task.metadata_json
    assert task_id in {task.id for task in claimable}
    assert wakeups == [task_id]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reject_overrun_stops_frozen_tasks_and_records_actor(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_root_item import RuntimeRootItem
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="require_confirmation")
    task_id = uuid.uuid4()
    source_agent_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="workflow",
                status="pending",
                budget_run_id=run.id,
                budget_admission_status="waiting_budget_approval",
                root_runtime_task_id=task_id,
            )
        )
        await db.flush()
        db.add(
            RuntimeRootItem(
                tenant_id=tenant_id,
                root_runtime_task_id=task_id,
                runtime_task_id=task_id,
                source_agent_id=source_agent_id,
                intent_key=f"workflow:{task_id}",
                work_type="workflow",
                target_ref=f"workflow:{task_id}",
                path_json=[f"agent:{source_agent_id}", f"workflow:{task_id}"],
                state="waiting_approval",
                admission_disposition="deferred",
                approval_ref=f"runtime-budget://{run.id}/reservation/workflow:{task_id}",
            )
        )
        stored_run = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        approval_episode_id = _mark_waiting_episode(stored_run)
        await db.commit()

    actor_id = uuid.uuid4()
    rejected = await service.reject_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        reason="owner declined further work",
        approval_episode_id=approval_episode_id,
        actor_user_id=actor_id,
    )

    async with owner_sessionmaker() as db:
        stopped_task = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == task_id))).scalar_one()
        stopped_root_item = (
            await db.execute(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == task_id))
        ).scalar_one()
        event = (
            await db.execute(
                select(RuntimeBudgetEvent).where(
                    RuntimeBudgetEvent.budget_run_id == run.id,
                    RuntimeBudgetEvent.event_type == "overrun_rejected",
                )
            )
        ).scalar_one()
    assert rejected is not None
    assert rejected.status == "stopped"
    assert stopped_task.status == "killed"
    assert stopped_task.budget_admission_status == "rejected"
    assert stopped_task.budget_terminal_reason == "runtime_budget_approval_rejected"
    assert stopped_root_item.state == "not_admitted"
    assert stopped_root_item.admission_disposition == "not_admitted"
    assert stopped_root_item.reason_code == "runtime_budget_approval_rejected"
    assert stopped_root_item.approval_ref == f"runtime-budget-event://{event.id}"
    assert event.reason == "owner declined further work"
    assert event.metadata_json["actor_user_id"] == str(actor_id)


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reject_overrun_is_exactly_idempotent_and_cannot_stop_an_approved_run(
    owner_sessionmaker,
    monkeypatch,
):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetService, RuntimeBudgetStateConflict

    async def ignore_worker_wake(**_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        ignore_worker_wake,
    )
    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    actor_id = uuid.uuid4()

    rejected_run = await _create_run(service, tenant_id, fail_mode="require_confirmation")
    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeBudgetRun, rejected_run.id)
        assert stored is not None
        rejected_episode_id = _mark_waiting_episode(stored)
        await db.commit()

    first = await service.reject_overrun(
        tenant_id=tenant_id,
        budget_run_id=rejected_run.id,
        reason="owner declined further work",
        approval_episode_id=rejected_episode_id,
        actor_user_id=actor_id,
    )
    replay = await service.reject_overrun(
        tenant_id=tenant_id,
        budget_run_id=rejected_run.id,
        reason="owner declined further work",
        approval_episode_id=rejected_episode_id,
        actor_user_id=actor_id,
    )
    assert first is not None and replay is not None
    assert first.id == replay.id and replay.status == "stopped"

    with pytest.raises(RuntimeBudgetStateConflict, match="is stopped"):
        await service.reject_overrun(
            tenant_id=tenant_id,
            budget_run_id=rejected_run.id,
            reason="different stale decision",
            approval_episode_id=rejected_episode_id,
            actor_user_id=actor_id,
        )
    async with owner_sessionmaker() as db:
        rejection_events = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == rejected_run.id,
                        RuntimeBudgetEvent.event_type == "overrun_rejected",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rejection_events) == 1

    approved_run = await _create_run(service, tenant_id, fail_mode="require_confirmation")
    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeBudgetRun, approved_run.id)
        assert stored is not None
        approved_episode_id = _mark_waiting_episode(stored)
        await db.commit()
    approved = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=approved_run.id,
        reason="owner approved continuation",
        approval_episode_id=approved_episode_id,
        actor_user_id=actor_id,
    )
    assert approved is not None and approved.status == "active"

    with pytest.raises(RuntimeBudgetStateConflict, match="is active"):
        await service.reject_overrun(
            tenant_id=tenant_id,
            budget_run_id=approved_run.id,
            reason="stale rejection from another tab",
            approval_episode_id=approved_episode_id,
            actor_user_id=actor_id,
        )
    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeBudgetRun, approved_run.id)
        rejection_event = await db.scalar(
            select(RuntimeBudgetEvent.id).where(
                RuntimeBudgetEvent.budget_run_id == approved_run.id,
                RuntimeBudgetEvent.event_type == "overrun_rejected",
            )
        )
    assert stored is not None and stored.status == "active"
    assert stored.terminal_reason is None and stored.completed_at is None
    assert rejection_event is None


@pytest.mark.usefixtures("migrated_pg_url")
async def test_approve_overrun_binds_exact_replay_to_current_episode(owner_sessionmaker, monkeypatch):
    from app.models.runtime_budget import RuntimeBudgetEvent
    from app.services.runtime_budget_service import (
        RuntimeBudgetApprovalRequired,
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetStateConflict,
    )

    async def ignore_worker_wake(**_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        ignore_worker_wake,
    )
    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="require_confirmation", max_subagents=0)
    actor_id = uuid.uuid4()
    first_reservation = RuntimeBudgetReservation(
        budget_run_id=run.id,
        reservation_key="approval-episode-a",
        subagents=1,
    )
    with pytest.raises(RuntimeBudgetApprovalRequired):
        await service.reserve(first_reservation)
    episode_a = await _current_episode(service, tenant_id, run.id)
    async with owner_sessionmaker() as db:
        denial_a = await db.scalar(
            select(RuntimeBudgetEvent).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == "approval-episode-a",
                RuntimeBudgetEvent.event_type == "denial",
            )
        )
    assert denial_a is not None and denial_a.id == episode_a

    first = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        approval_episode_id=episode_a,
        reason="approve episode",
        actor_user_id=actor_id,
    )
    replay = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        approval_episode_id=episode_a,
        reason="approve episode",
        actor_user_id=actor_id,
    )
    assert first is not None and replay is not None and replay.status == "active"
    await service.reserve(first_reservation)
    with pytest.raises(RuntimeBudgetApprovalRequired):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key="approval-episode-b",
                subagents=1,
            )
        )
    episode_b = await _current_episode(service, tenant_id, run.id)
    assert episode_b != episode_a

    with pytest.raises(RuntimeBudgetStateConflict, match="approval episode conflict"):
        await service.approve_overrun(
            tenant_id=tenant_id,
            budget_run_id=run.id,
            approval_episode_id=episode_a,
            reason="approve episode",
            actor_user_id=actor_id,
        )
    current = await service.get_run(tenant_id=tenant_id, budget_run_id=run.id)
    assert current is not None and current.status == "waiting_budget_approval"
    assert current.approval_episode_id == episode_b

    approved_b = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        approval_episode_id=episode_b,
        reason="approve episode b",
        actor_user_id=actor_id,
    )
    replay_b = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        approval_episode_id=episode_b,
        reason="approve episode b",
        actor_user_id=actor_id,
    )
    assert approved_b is not None and replay_b is not None and replay_b.status == "active"
    async with owner_sessionmaker() as db:
        approvals = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent)
                    .where(
                        RuntimeBudgetEvent.budget_run_id == run.id,
                        RuntimeBudgetEvent.event_type == "overrun_approved",
                    )
                    .order_by(RuntimeBudgetEvent.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert [event.metadata_json["approval_episode_id"] for event in approvals] == [
        str(episode_a),
        str(episode_b),
    ]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reject_overrun_binds_exact_replay_to_current_episode(owner_sessionmaker, monkeypatch):
    from app.models.runtime_budget import RuntimeBudgetEvent
    from app.services.runtime_budget_service import (
        RuntimeBudgetApprovalRequired,
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetStateConflict,
    )

    async def ignore_worker_wake(**_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        ignore_worker_wake,
    )
    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="require_confirmation", max_subagents=0)
    actor_id = uuid.uuid4()
    first_reservation = RuntimeBudgetReservation(
        budget_run_id=run.id,
        reservation_key="rejection-episode-a",
        subagents=1,
    )
    with pytest.raises(RuntimeBudgetApprovalRequired):
        await service.reserve(first_reservation)
    episode_a = await _current_episode(service, tenant_id, run.id)
    await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        approval_episode_id=episode_a,
        reason="approve episode a",
        actor_user_id=actor_id,
    )
    await service.reserve(first_reservation)
    with pytest.raises(RuntimeBudgetApprovalRequired):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key="rejection-episode-b",
                subagents=1,
            )
        )
    episode_b = await _current_episode(service, tenant_id, run.id)
    assert episode_b != episode_a

    with pytest.raises(RuntimeBudgetStateConflict, match="approval episode conflict"):
        await service.reject_overrun(
            tenant_id=tenant_id,
            budget_run_id=run.id,
            approval_episode_id=episode_a,
            reason="reject stale episode a",
            actor_user_id=actor_id,
        )
    rejected = await service.reject_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        approval_episode_id=episode_b,
        reason="reject episode b",
        actor_user_id=actor_id,
    )
    replay = await service.reject_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        approval_episode_id=episode_b,
        reason="reject episode b",
        actor_user_id=actor_id,
    )
    assert rejected is not None and replay is not None and replay.status == "stopped"
    async with owner_sessionmaker() as db:
        rejections = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == run.id,
                        RuntimeBudgetEvent.event_type == "overrun_rejected",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rejections) == 1
    assert rejections[0].metadata_json["approval_episode_id"] == str(episode_b)


@pytest.mark.usefixtures("migrated_pg_url")
async def test_waiting_legacy_run_derives_and_persists_episode_from_transition_event(
    owner_sessionmaker,
    monkeypatch,
):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetService

    async def ignore_worker_wake(**_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        ignore_worker_wake,
    )
    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="require_confirmation")
    episode_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeBudgetRun, run.id)
        assert stored is not None
        stored.status = "waiting_budget_approval"
        stored.terminal_reason = "runtime_budget_approval_required:subagents"
        stored.metadata_json = {}
        db.add(
            RuntimeBudgetEvent(
                id=episode_id,
                tenant_id=tenant_id,
                budget_run_id=run.id,
                event_type="denial",
                reservation_key="legacy-waiting-episode",
                allowed=False,
                would_deny=True,
                reason="legacy waiting event",
                amounts_json={},
                metadata_json={"target_status": "waiting_budget_approval"},
            )
        )
        await db.commit()

    exposed = await service.get_run(tenant_id=tenant_id, budget_run_id=run.id)
    assert exposed is not None and exposed.approval_episode_id == episode_id
    approved = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        approval_episode_id=episode_id,
        reason="approve legacy waiting episode",
    )
    assert approved is not None and approved.status == "active"
    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeBudgetRun, run.id)
    assert stored is not None
    assert stored.metadata_json["approval_episode_id"] == str(episode_id)


@pytest.mark.usefixtures("migrated_pg_url")
async def test_approval_raises_limit_for_unbound_foreground_retry(owner_sessionmaker, monkeypatch):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services import runtime_task_worker
    from app.services.runtime_budget_service import (
        RuntimeBudgetApprovalRequired,
        RuntimeBudgetReservation,
        RuntimeBudgetService,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="require_confirmation", max_subagents=0)
    reservation = RuntimeBudgetReservation(
        budget_run_id=run.id,
        reservation_key="foreground-retry",
        subagents=1,
        reason="foreground child",
    )
    with pytest.raises(RuntimeBudgetApprovalRequired):
        await service.reserve(reservation)

    wakeups = []

    async def fake_notify_runtime_task_worker(*, reason, runtime_task_id=None):
        wakeups.append((reason, runtime_task_id))

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", fake_notify_runtime_task_worker)

    approval_episode_id = await _current_episode(service, tenant_id, run.id)
    approved = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        reason="allow one foreground retry",
        approval_episode_id=approval_episode_id,
        actor_user_id=uuid.uuid4(),
    )
    retried = await service.reserve(reservation)

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
    assert approved is not None
    assert approved.max_subagents == 1
    assert retried.allowed is True
    assert stored.reserved_subagents == 1
    assert wakeups == [("runtime_budget_approved", None)]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_settlement_is_idempotent_and_does_not_double_count(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import (
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetSettlement,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_background_tasks=3)
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="exactly-once",
            background_tasks=1,
        )
    )
    settlement = RuntimeBudgetSettlement(
        budget_run_id=run.id,
        reservation_key="exactly-once",
        actual_background_tasks=1,
        reason="done",
    )
    await service.settle(settlement)
    await service.settle(settlement)

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == run.id,
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert stored.reserved_background_tasks == 0
    assert stored.used_background_tasks == 1
    assert len(settlements) == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_settlement_without_reservation_is_rejected_without_usage_mutation(owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import (
        RuntimeBudgetService,
        RuntimeBudgetSettlement,
        RuntimeBudgetSettlementConflict,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_background_tasks=3)

    with pytest.raises(RuntimeBudgetSettlementConflict, match="reservation missing"):
        await service.settle(
            RuntimeBudgetSettlement(
                budget_run_id=run.id,
                reservation_key="never-reserved",
                actual_background_tasks=1,
                reason="invalid settlement",
            )
        )

    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeBudgetRun, run.id)
        settlement = await db.scalar(
            select(RuntimeBudgetEvent.id).where(
                RuntimeBudgetEvent.budget_run_id == run.id,
                RuntimeBudgetEvent.reservation_key == "never-reserved",
                RuntimeBudgetEvent.event_type == "settlement",
            )
        )
    assert stored is not None and stored.used_background_tasks == 0
    assert settlement is None

    async with owner_sessionmaker() as db:
        db.add(
            RuntimeBudgetEvent(
                tenant_id=tenant_id,
                budget_run_id=run.id,
                event_type="settlement",
                reservation_key="legacy-orphan-settlement",
                allowed=True,
                would_deny=False,
                amounts_json={"background_tasks": 1},
            )
        )
        await db.commit()
    with pytest.raises(RuntimeBudgetSettlementConflict, match="reservation missing"):
        await service.settle(
            RuntimeBudgetSettlement(
                budget_run_id=run.id,
                reservation_key="legacy-orphan-settlement",
                actual_background_tasks=1,
            )
        )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_settlement_replay_rejects_different_actual_usage(owner_sessionmaker):
    from app.services.runtime_budget_service import (
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetSettlement,
        RuntimeBudgetSettlementConflict,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_background_tasks=3)
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="conflicting-replay",
            background_tasks=1,
        )
    )
    await service.settle(
        RuntimeBudgetSettlement(
            budget_run_id=run.id,
            reservation_key="conflicting-replay",
            actual_background_tasks=1,
        )
    )

    with pytest.raises(RuntimeBudgetSettlementConflict, match="settlement actuals conflict"):
        await service.settle(
            RuntimeBudgetSettlement(
                budget_run_id=run.id,
                reservation_key="conflicting-replay",
                actual_background_tasks=2,
            )
        )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_settlement_binds_reservation_key_to_one_runtime_task(owner_sessionmaker):
    from app.services.runtime_budget_service import (
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetSettlement,
        RuntimeBudgetSettlementConflict,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, max_background_tasks=3)
    reserved_task_id = uuid.uuid4()
    other_task_id = uuid.uuid4()
    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="task-bound-reservation",
            background_tasks=1,
            runtime_task_id=reserved_task_id,
        )
    )

    with pytest.raises(RuntimeBudgetSettlementConflict, match="reservation runtime task conflict"):
        await service.settle(
            RuntimeBudgetSettlement(
                budget_run_id=run.id,
                reservation_key="task-bound-reservation",
                actual_background_tasks=1,
                runtime_task_id=other_task_id,
            )
        )

    await service.settle(
        RuntimeBudgetSettlement(
            budget_run_id=run.id,
            reservation_key="task-bound-reservation",
            actual_background_tasks=1,
            runtime_task_id=reserved_task_id,
        )
    )
    with pytest.raises(RuntimeBudgetSettlementConflict, match="settlement runtime task conflict"):
        await service.settle(
            RuntimeBudgetSettlement(
                budget_run_id=run.id,
                reservation_key="task-bound-reservation",
                actual_background_tasks=1,
                runtime_task_id=other_task_id,
            )
        )


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


@pytest.mark.usefixtures("migrated_pg_url")
async def test_summary_lane_admits_finalization_provider_call_despite_exhausted_tokens(owner_sessionmaker):
    """§2 finalization lane: a summary_only run admits the summarizing turn's
    provider calls even when the token dimension is exhausted; usage still settles."""
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import (
        RuntimeBudgetDenied,
        RuntimeBudgetReservation,
        RuntimeBudgetService,
        RuntimeBudgetSettlement,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="summary_only", max_tokens=1_000)

    # Exhaust the token dimension: the run trips into summary_only.
    await service.reserve(
        RuntimeBudgetReservation(budget_run_id=run.id, reservation_key="burn", tokens=1_000, reason="burn")
    )
    with pytest.raises(RuntimeBudgetDenied):
        await service.reserve(
            RuntimeBudgetReservation(budget_run_id=run.id, reservation_key="over", tokens=100, reason="over budget")
        )
    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
    assert stored.status == "summary_only"

    # A plain provider call is still denied (tokens exhausted, no lane marker)...
    with pytest.raises(RuntimeBudgetDenied):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id, reservation_key="plain-call", tokens=100, provider_calls=1, reason="plain"
            )
        )

    # ...but the summarizing turn's call carries the lane marker and is admitted.
    lane_result = await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id,
            reservation_key="summary-call",
            tokens=100,
            provider_calls=1,
            reason="provider_call_start",
            metadata={"budget_summary_turn": True},
        )
    )
    assert lane_result.allowed is True

    # Work amplification stays denied even with the lane marker.
    with pytest.raises(RuntimeBudgetDenied):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key="summary-fanout",
                subagents=1,
                reason="fanout in summary turn",
                metadata={"budget_summary_turn": True},
            )
        )

    # Usage still settles honestly for accounting.
    await service.settle(
        RuntimeBudgetSettlement(
            budget_run_id=run.id,
            reservation_key="summary-call",
            actual_tokens=80,
            actual_provider_calls=1,
            reason="provider_call_completed",
        )
    )
    async with owner_sessionmaker() as db:
        events = (
            (
                await db.execute(
                    select(RuntimeBudgetEvent)
                    .where(
                        RuntimeBudgetEvent.budget_run_id == run.id, RuntimeBudgetEvent.reservation_key == "summary-call"
                    )
                    .order_by(RuntimeBudgetEvent.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert [event.event_type for event in events] == ["reservation", "settlement"]
    assert events[0].reason == "budget_summary_turn_lane"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_hard_stop_run_closes_summary_only_lane(owner_sessionmaker):
    """§2: after the finalization turn the summary_only lane is sealed for good."""
    from app.services.runtime_budget_service import (
        RuntimeBudgetDenied,
        RuntimeBudgetReservation,
        RuntimeBudgetService,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="summary_only", max_tokens=1_000)
    with pytest.raises(RuntimeBudgetDenied):
        await service.reserve(
            RuntimeBudgetReservation(budget_run_id=run.id, reservation_key="trip", tokens=5_000, reason="trip")
        )

    stopped = await service.hard_stop_run(tenant_id=tenant_id, budget_run_id=run.id, reason="budget_summary_completed")
    assert stopped is not None
    assert stopped.status == "hard_stopped"

    # Even the lane marker cannot reopen a hard-stopped run.
    with pytest.raises(RuntimeBudgetDenied):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key="after-stop",
                tokens=10,
                reason="post-stop",
                metadata={"budget_summary_turn": True},
            )
        )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_mark_summary_turn_state_is_a_single_winner_cas(owner_sessionmaker):
    """§2 exactly-once: only the first transition from empty wins; repeats lose."""
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="summary_only")

    first = await service.mark_summary_turn_state(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        expected_states=(None,),
        new_state="issued",
        extra={"summary_run_id": "run-a"},
    )
    assert first is True
    # A concurrent second issuer loses the CAS.
    second = await service.mark_summary_turn_state(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        expected_states=(None,),
        new_state="issued",
        extra={"summary_run_id": "run-b"},
    )
    assert second is False
    # Legal follow-up transition: issued -> retried.
    retried = await service.mark_summary_turn_state(
        tenant_id=tenant_id,
        budget_run_id=run.id,
        expected_states=("issued",),
        new_state="retried",
    )
    assert retried is True

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
    metadata = dict(stored.metadata_json or {})
    assert metadata.get("summary_turn_state") == "retried"
    assert metadata.get("summary_run_id") == "run-a"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_duplicate_terminal_denial_reservation_replays_typed_truth_without_duplicate_event(
    app_user_sessionmaker,
    owner_sessionmaker,
):
    from sqlalchemy import select

    from app.models.runtime_budget import RuntimeBudgetEvent
    from app.services.runtime_budget_service import (
        RuntimeBudgetDenied,
        RuntimeBudgetReservation,
        RuntimeBudgetService,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run = await _create_run(service, tenant_id)

    # Reach a terminal (hard_stopped) budget state, then replay the same
    # reservation key against it — the denial must replay the recorded typed
    # truth exactly once (no duplicate event, no unique-constraint failure).
    from app.models.runtime_budget import RuntimeBudgetRun

    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id).with_for_update())
        ).scalar_one()
        stored.status = "hard_stopped"
        stored.terminal_reason = "runtime_budget_circuit_break:team_sessions:1>=0"
        await db.commit()

    reservation = RuntimeBudgetReservation(
        budget_run_id=run.id,
        reservation_key="page-continuation-lane",
        provider_calls=1,
        reason="result page continuation admission",
    )
    with pytest.raises(RuntimeBudgetDenied) as first_denial:
        await service.reserve(reservation)
    with pytest.raises(RuntimeBudgetDenied) as replay_denial:
        await service.reserve(reservation)

    assert str(replay_denial.value) == str(first_denial.value)
    assert str(first_denial.value) == "runtime_budget_circuit_break:team_sessions:1>=0"

    async with owner_sessionmaker() as db:
        denials = (
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == run.id,
                        RuntimeBudgetEvent.event_type == "denial",
                        RuntimeBudgetEvent.reservation_key == "page-continuation-lane",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(denials) == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_duplicate_summary_only_plain_denial_replays_typed_truth(
    app_user_sessionmaker,
    owner_sessionmaker,
):
    from sqlalchemy import select

    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import (
        RuntimeBudgetDenied,
        RuntimeBudgetReservation,
        RuntimeBudgetService,
    )

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run = await _create_run(service, tenant_id, max_tokens=100)

    # A summary_only run denying a plain (non-work-amplifying) reservation
    # through the exhausted-dimensions path: replaying the same key must
    # replay the identical typed denial, never a second insert.
    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id).with_for_update())
        ).scalar_one()
        stored.status = "summary_only"
        stored.terminal_reason = "runtime_budget_summary_only:tokens"
        await db.commit()

    reservation = RuntimeBudgetReservation(
        budget_run_id=run.id,
        reservation_key="plain-token-lane",
        tokens=500,
        reason="plain token reservation",
    )
    with pytest.raises(RuntimeBudgetDenied) as first_denial:
        await service.reserve(reservation)
    with pytest.raises(RuntimeBudgetDenied) as replay_denial:
        await service.reserve(reservation)

    assert str(replay_denial.value) == str(first_denial.value)
    assert replay_denial.value.dimensions == first_denial.value.dimensions
    assert first_denial.value.dimensions == ["tokens"]

    async with owner_sessionmaker() as db:
        denials = (
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == run.id,
                        RuntimeBudgetEvent.event_type == "denial",
                        RuntimeBudgetEvent.reservation_key == "plain-token-lane",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(denials) == 1
