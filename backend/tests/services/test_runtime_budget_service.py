from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta

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
