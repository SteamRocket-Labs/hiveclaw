"""§10 circuit-breaker coverage for the runtime budget control plane.

Two layers:

* Pure-function unit tests for ``evaluate_circuit_breaker`` /
  ``_breaker_status_for_fail_mode`` / builtin defaults — these run without a
  database (functional core, no mocks) and are the red→green TDD centerpiece.
* DB-integration tests for the settlement-post breaker, the parent-wake breaker,
  ground-truth counter materialization, and pending-work cancellation — these
  use the real Postgres fixture and are skipped when Docker is unavailable
  (they run in CI). The concurrency/atomicity guarantees they lean on are the
  same ones the existing ``test_runtime_budget_service`` PG suite verifies.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select


# ---------------------------------------------------------------------------
# Pure-function unit tests (no database)
# ---------------------------------------------------------------------------


def _maxes(**overrides):
    base = {
        "tokens": None,
        "cache_miss_tokens": None,
        "subagents": None,
        "team_sessions": None,
        "delegations": None,
        "background_tasks": None,
        "continuation_wakes": None,
        "provider_calls": None,
        "parent_invocations": None,
        "failures": None,
        "needs_reconciliation": None,
        "child_failure_ratio": None,
    }
    base.update(overrides)
    return base


def test_breaker_status_for_fail_mode_maps_documented_modes():
    from app.services.runtime_budget_service import _breaker_status_for_fail_mode

    assert _breaker_status_for_fail_mode("summary_only") == "summary_only"
    assert _breaker_status_for_fail_mode("require_confirmation") == "summary_only"
    assert _breaker_status_for_fail_mode("hard_stop") == "hard_stopped"
    assert _breaker_status_for_fail_mode("fail_closed") == "hard_stopped"
    assert _breaker_status_for_fail_mode(None) == "hard_stopped"


def test_breaker_does_not_trip_when_all_maxes_none():
    from app.services.runtime_budget_service import evaluate_circuit_breaker

    tripped = evaluate_circuit_breaker(
        used={"subagents": 999, "provider_calls": 999},
        reserved={},
        maxes=_maxes(),
        failures=999,
        needs_reconciliation_count=999,
        parent_invocations=999,
    )
    assert tripped == []


def test_breaker_trips_on_failures_at_or_over_max():
    from app.services.runtime_budget_service import evaluate_circuit_breaker

    assert (
        evaluate_circuit_breaker(
            used={},
            reserved={},
            maxes=_maxes(failures=5),
            failures=4,
            needs_reconciliation_count=0,
            parent_invocations=0,
        )
        == []
    )
    tripped = evaluate_circuit_breaker(
        used={},
        reserved={},
        maxes=_maxes(failures=5),
        failures=5,
        needs_reconciliation_count=0,
        parent_invocations=0,
    )
    assert any(reason.startswith("failures:") for reason in tripped)


def test_breaker_trips_on_needs_reconciliation_at_or_over_max():
    from app.services.runtime_budget_service import evaluate_circuit_breaker

    tripped = evaluate_circuit_breaker(
        used={},
        reserved={},
        maxes=_maxes(needs_reconciliation=3),
        failures=0,
        needs_reconciliation_count=3,
        parent_invocations=0,
    )
    assert any(reason.startswith("needs_reconciliation:") for reason in tripped)


def test_breaker_trips_on_parent_invocations_at_or_over_max():
    from app.services.runtime_budget_service import evaluate_circuit_breaker

    tripped = evaluate_circuit_breaker(
        used={},
        reserved={},
        maxes=_maxes(parent_invocations=16),
        failures=0,
        needs_reconciliation_count=0,
        parent_invocations=16,
    )
    assert any(reason.startswith("parent_invocations:") for reason in tripped)


def test_child_failure_ratio_requires_minimum_children():
    from app.services.runtime_budget_service import evaluate_circuit_breaker

    # 4/4 = 1.0 ratio but only 4 children (< min 8) → not enough signal, no trip.
    assert (
        evaluate_circuit_breaker(
            used={},
            reserved={},
            maxes=_maxes(child_failure_ratio=0.5),
            failures=4,
            needs_reconciliation_count=0,
            parent_invocations=0,
            child_failure_ratio=1.0,
            total_children=4,
        )
        == []
    )
    # 5/8 = 0.625 >= 0.5 with enough children → trip.
    tripped = evaluate_circuit_breaker(
        used={},
        reserved={},
        maxes=_maxes(child_failure_ratio=0.5),
        failures=5,
        needs_reconciliation_count=0,
        parent_invocations=0,
        child_failure_ratio=0.625,
        total_children=8,
    )
    assert any(reason.startswith("child_failure_ratio:") for reason in tripped)


def test_token_dimension_counts_reserved_plus_used_but_counts_use_used_only():
    from app.services.runtime_budget_service import evaluate_circuit_breaker

    # tokens: used 600 + reserved 500 = 1100 >= 1000 → trips.
    tripped = evaluate_circuit_breaker(
        used={"tokens": 600, "subagents": 1},
        reserved={"tokens": 500},
        maxes=_maxes(tokens=1000, subagents=4),
        failures=0,
        needs_reconciliation_count=0,
        parent_invocations=0,
    )
    assert any(reason.startswith("tokens:") for reason in tripped)
    # subagents: used 1 (reserved ignored for count dims) < 4 → no subagent trip.
    assert not any(reason.startswith("subagents:") for reason in tripped)


def test_builtin_defaults_carry_breaker_dimensions():
    from app.services.runtime_budget_service import RuntimeBudgetPolicyLookup, _builtin_policy

    expected_parent = {"interactive": 16, "scheduled": 16, "workflow": 64, "agent_team": 24}
    for profile, parent in expected_parent.items():
        policy = _builtin_policy(RuntimeBudgetPolicyLookup(tenant_id=uuid.uuid4(), profile=profile))
        assert policy.max_failures == 5
        assert policy.max_needs_reconciliation == 3
        assert policy.max_child_failure_ratio == 0.5
        assert policy.max_parent_invocations == parent


# ---------------------------------------------------------------------------
# DB-integration tests (real Postgres; skipped without Docker, run in CI)
# ---------------------------------------------------------------------------


async def _seed_tenant(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tenant_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Breaker Tenant", slug=f"breaker-{tenant_id.hex[:8]}"))
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
        "fail_mode": "summary_only",
        "max_tokens": 10_000,
        "max_provider_calls": 5,
        "max_subagents": 32,
        "enforcement_mode": "enforce",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    payload.update(overrides)
    return await service.create_run(RuntimeBudgetRunCreate(**payload))


async def _add_child_tasks(owner_sessionmaker, *, tenant_id, budget_run_id, statuses):
    from app.models.runtime_task import RuntimeTask

    async with owner_sessionmaker() as db:
        for status in statuses:
            db.add(
                RuntimeTask(
                    id=uuid.uuid4(),
                    task_type="subagent",
                    tenant_id=tenant_id,
                    budget_run_id=budget_run_id,
                    status=status,
                )
            )
        await db.commit()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_wake_breaker_summary_only_after_consecutive_failures(
    app_user_sessionmaker,
    owner_sessionmaker,
):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="summary_only", max_failures=2)
    await _add_child_tasks(
        owner_sessionmaker,
        tenant_id=tenant_id,
        budget_run_id=run.id,
        statuses=["failed", "failed", "failed"],
    )

    reason = await service.evaluate_wake_breaker(tenant_id=tenant_id, budget_run_id=run.id)

    assert reason is not None and "failures" in reason
    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        events = (
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == run.id,
                        RuntimeBudgetEvent.event_type == "circuit_break",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert stored.status == "summary_only"
    assert stored.failures == 3  # materialized from ground truth
    assert stored.parent_invocations == 1
    assert len(events) == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_wake_breaker_trips_on_needs_reconciliation(
    app_user_sessionmaker,
    owner_sessionmaker,
):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="hard_stop", max_needs_reconciliation=2)
    await _add_child_tasks(
        owner_sessionmaker,
        tenant_id=tenant_id,
        budget_run_id=run.id,
        statuses=["needs_reconciliation", "needs_reconciliation"],
    )

    reason = await service.evaluate_wake_breaker(tenant_id=tenant_id, budget_run_id=run.id)

    assert reason is not None and "needs_reconciliation" in reason
    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
    assert stored.status == "hard_stopped"
    assert stored.needs_reconciliation_count == 2


@pytest.mark.usefixtures("migrated_pg_url")
async def test_wake_breaker_counts_parent_invocations_and_stops_at_max(
    app_user_sessionmaker,
    owner_sessionmaker,
):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="hard_stop", max_parent_invocations=2)

    # No failed children — only the parent-invocation counter should trip it.
    first = await service.evaluate_wake_breaker(tenant_id=tenant_id, budget_run_id=run.id)
    assert first is None  # parent_invocations == 1 < 2
    second = await service.evaluate_wake_breaker(tenant_id=tenant_id, budget_run_id=run.id)
    assert second is not None and "parent_invocations" in second

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
    assert stored.status == "hard_stopped"
    assert stored.parent_invocations == 2


@pytest.mark.usefixtures("migrated_pg_url")
async def test_settlement_trips_breaker_when_provider_calls_exhausted(
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
    run = await _create_run(service, tenant_id, fail_mode="hard_stop", max_provider_calls=1, max_tokens=100_000)

    await service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=run.id, reservation_key="call-1", provider_calls=1, tokens=500, reason="call"
        )
    )
    await service.settle(
        RuntimeBudgetSettlement(
            budget_run_id=run.id, reservation_key="call-1", actual_provider_calls=1, actual_tokens=450, reason="settled"
        )
    )

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))).scalar_one()
        breaker_events = (
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == run.id,
                        RuntimeBudgetEvent.event_type == "circuit_break",
                    )
                )
            )
            .scalars()
            .all()
        )
    # used_provider_calls (1) >= max_provider_calls (1) → breaker trips after settle.
    assert stored.status == "hard_stopped"
    assert len(breaker_events) == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_wake_breaker_hard_stop_cancels_pending_tasks_under_budget(
    app_user_sessionmaker,
    owner_sessionmaker,
):
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run = await _create_run(service, tenant_id, fail_mode="hard_stop", max_failures=1)

    await _add_child_tasks(
        owner_sessionmaker,
        tenant_id=tenant_id,
        budget_run_id=run.id,
        statuses=["failed"],
    )
    # A queued, unclaimed sibling task under the same budget run.
    pending_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=pending_id,
                task_type="subagent",
                tenant_id=tenant_id,
                budget_run_id=run.id,
                status="pending",
            )
        )
        await db.commit()

    reason = await service.evaluate_wake_breaker(tenant_id=tenant_id, budget_run_id=run.id)
    assert reason is not None

    async with owner_sessionmaker() as db:
        pending = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == pending_id))).scalar_one()
    assert pending.status == "killed"
    assert pending.budget_admission_status == "cancelled"
