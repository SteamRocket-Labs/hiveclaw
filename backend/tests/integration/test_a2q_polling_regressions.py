"""Actual PostgreSQL selection and delivery-readiness regressions from A2q review."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.services import budget_transition_outbox as budget
from app.services import runtime_task_worker as worker
from tests.services.test_runtime_terminal_boundary_outbox import _seed_terminal_task


@pytest.fixture
async def addressed(owner_sessionmaker):
    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="A2q r2 predicates")
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.task_id)
        task.terminal_boundary_enqueued_at = task.completion_outbox_settled_at = datetime.now(UTC)
        run = RuntimeBudgetRun(
            id=uuid4(),
            tenant_id=seed.tenant_id,
            root_run_kind="fixture",
            root_run_key=uuid4().hex,
            root_agent_id=seed.agent_id,
            root_session_id=str(seed.session_id),
            root_user_id=seed.user_id,
        )
        db.add(run)
        await db.commit()
    yield seed, run.id
    # Global probes in subsequent tests must not see this test's intentionally
    # unpersisted producer doubles. Only quiesce this fixture's own budget.
    async with owner_sessionmaker() as db:
        (await db.get(RuntimeBudgetRun, run.id)).root_agent_id = None
        await db.commit()


async def _observe_reconcile(factory, monkeypatch, tenant_id, *, limit):
    selected, produced = [], []

    @asynccontextmanager
    async def observed_factory():
        async with factory() as db:
            execute = db.execute

            async def observed(statement, *args, **kwargs):
                result = await execute(statement, *args, **kwargs)
                if any(d.get("entity") is RuntimeBudgetEvent for d in getattr(statement, "column_descriptions", ())):
                    frozen = result.freeze()
                    selected.extend(row[0] for row in frozen())
                    return frozen()
                return result

            monkeypatch.setattr(db, "execute", observed)
            yield db

    async def produce(_db, **kwargs):
        produced.append(kwargs["event"].id)
        return uuid4()  # Producer effects are tested in test_budget_transition_outbox.

    monkeypatch.setattr(budget, "enqueue_budget_transition", produce)
    result = await budget.BudgetTransitionOutboxService(session_factory=observed_factory).reconcile_budget_events_once(
        tenant_id=tenant_id,
        limit=limit,
    )
    assert result == len(produced)
    return selected, produced


async def test_budget_legacy_denials_do_not_starve_a_padded_valid_target(addressed, owner_sessionmaker, monkeypatch):
    seed, run_id = addressed
    async with owner_sessionmaker() as db:
        # The four production rows have this exact relevant shape: denial,
        # target_status absent, no outbox, root Agent/session present. More
        # than a page proves the old order/limit cannot starve newer work.
        for _ in range(104):
            db.add(
                RuntimeBudgetEvent(
                    id=uuid4(),
                    tenant_id=seed.tenant_id,
                    budget_run_id=run_id,
                    event_type="denial",
                    metadata_json={},
                    created_at=datetime(2026, 9, 9, tzinfo=UTC),
                )
            )
        valid = RuntimeBudgetEvent(
            id=uuid4(),
            tenant_id=seed.tenant_id,
            budget_run_id=run_id,
            event_type="denial",
            metadata_json={"target_status": "\t\u3000summary_only\u001c\n"},
        )
        db.add(valid)
        await db.commit()
    selected, produced = await _observe_reconcile(owner_sessionmaker, monkeypatch, seed.tenant_id, limit=100)
    assert [row.id for row in selected] == [valid.id]
    assert produced == [valid.id]


async def test_budget_sql_is_a_superset_of_python_strip_for_all_whitespace_and_json_targets(
    addressed, owner_sessionmaker, monkeypatch
):
    seed, run_id = addressed
    targets = [None, "", "invalid", 0, False, {}, [], ["summary_only"], "prefix_summary_only_suffix"]
    whitespace = [chr(code) for code in range(0x3100) if chr(code).isspace()]
    assert len(whitespace) == 29
    targets += [
        pad + status + pad
        for status in ("waiting_budget_approval", "summary_only", "exhausted", "hard_stopped")
        for pad in ["", *whitespace]
    ]
    rows = []
    async with owner_sessionmaker() as db:
        for event_type in (
            "denial",
            "circuit_break",
            "overrun_approved",
            "overrun_rejected",
            "expired",
            "cancelled",
            "other",
        ):
            for target in targets:
                row = RuntimeBudgetEvent(
                    id=uuid4(),
                    tenant_id=seed.tenant_id,
                    budget_run_id=run_id,
                    event_type=event_type,
                    metadata_json={} if target is None else {"target_status": target},
                )
                rows.append(row)
                db.add(row)
        await db.commit()
    selected, produced = await _observe_reconcile(owner_sessionmaker, monkeypatch, seed.tenant_id, limit=2000)
    selected_ids = {row.id for row in selected}
    accepted = {row.id for row in rows if budget.transition_for_event(row) is not None}
    assert accepted <= selected_ids
    assert set(produced) == accepted  # Python is still the final authority.
    absent = {row.id for row in rows if row.event_type in {"denial", "circuit_break"} and not row.metadata_json}
    assert selected_ids.isdisjoint(absent)
    # Explicit documented false positives: a JSON container or non-whitespace
    # prefix/suffix containing a valid target. None is projected by Python.
    false_positives = selected_ids - accepted
    expected_false_positives = {
        row.id
        for row in rows
        if row.event_type in {"denial", "circuit_break"}
        and row.metadata_json.get("target_status") in ("prefix_summary_only_suffix", ["summary_only"])
    }
    assert false_positives == expected_false_positives


async def test_notification_leg_honours_the_consumers_delivery_ready(addressed, owner_sessionmaker, monkeypatch):
    from app.services.runtime_notification_outbox import (
        enqueue_completion_notification,
        RuntimeNotificationOutboxService,
    )
    from app.services.runtime_terminal_boundary_outbox import enqueue_terminal_boundary
    from tests.services.test_runtime_notification_outbox import _notification

    seed, _run_id = addressed
    async with owner_sessionmaker() as db:
        notice = replace(
            _notification(
                tenant_id=seed.tenant_id, user_id=seed.user_id, agent_id=seed.agent_id, session_id=seed.session_id
            ),
            source_kind="trigger",
            task_type="trigger",
            source_run_id=str(seed.task_id),
            artifacts=[{"path": "runtime_artifacts/triggers/result.json"}],
        )
        identity = await enqueue_completion_notification(db, notice)
        row = await db.get(RuntimeNotificationOutbox, identity)
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    monkeypatch.setattr(worker, "async_session", owner_sessionmaker)
    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.claim_batch(worker_id="a2q-r2") == []
    result = await worker._has_due_runtime_work(worker_id="a2q-r2")
    # The bool compatibility branch lets this fail on f2f9d3fb for the actual
    # ignored-delivery-ready bug, not for a missing/new interface.
    assert (result["runtime-notification"] if isinstance(result, dict) else result) is False
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.task_id)
        boundary = await enqueue_terminal_boundary(
            db,
            task=task,
            event_kind="turn_stop",
            agent_id=seed.agent_id,
            session_id=seed.session_id,
            terminal_status="completed",
            authority_ref="runtime_task",
            authority_id=task.id,
            binding={},
        )
        boundary.status = "delivered"
        await db.commit()
    result = await worker._has_due_runtime_work(worker_id="a2q-r2")
    assert (result["runtime-notification"] if isinstance(result, dict) else result) is True
    assert [item.id for item in await service.claim_batch(worker_id="a2q-r2")] == [identity]
