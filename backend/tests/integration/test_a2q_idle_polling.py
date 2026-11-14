from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import logging
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.budget_transition_outbox import BudgetTransitionOutbox
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultIntegrationPage
from app.models.runtime_task import TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES, RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.models.session_v2 import (
    SessionCommand,
    SessionControlInput,
    SessionEventOutbox,
    SessionInputAdmission,
    SessionTurnInput,
    SessionTurnReplacement,
)
from app.services import runtime_task_worker as worker
from tests.integration import conftest as pg_fixtures
from tests.services.test_runtime_terminal_boundary_outbox import _seed_terminal_task


@pytest.fixture
async def idle_db(owner_engine, monkeypatch):
    # Rollback-only isolation: earlier service tests may leave due queues.
    # Quiesce only their existing worker states in this transaction, without
    # deleting immutable journals or changing any database enforcement.
    # All sessions share savepoints; the outer rollback restores every row.
    async with owner_engine.connect() as conn, conn.begin():
        for sql in (
            "UPDATE runtime_tasks SET scheduled_at=now()+interval '100 years', claim_expires_at=now()+interval '100 years', terminal_boundary_enqueued_at=now(), completion_outbox_settled_at=now()",
            "UPDATE runtime_terminal_boundary_outbox SET status='delivered'",
            "UPDATE session_event_outbox SET status='published'",
            "UPDATE channel_delivery_outbox SET status='delivered'",
            "UPDATE budget_transition_outbox SET status='delivered'",
            "UPDATE runtime_notification_outbox SET status='delivered'",
            "UPDATE runtime_result_integration_pages SET status='delivered'",
            "UPDATE runtime_budget_runs SET root_agent_id=NULL",
            "UPDATE session_input_admissions SET dispatch_state='not_applicable'",
            "UPDATE session_turn_replacements SET state='needs_reconciliation'",
            "UPDATE session_control_inputs SET status='rejected'",
        ):
            await conn.execute(text(sql))
        factory = async_sessionmaker(conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        seed = await _seed_terminal_task(factory, tenant_name="A2q idle")
        now = datetime.now(UTC)
        async with factory() as db:
            task = await db.get(RuntimeTask, seed.task_id)
            task.terminal_boundary_enqueued_at = now
            task.completion_outbox_settled_at = now
            await db.commit()

        @asynccontextmanager
        async def app_factory():
            await conn.execute(text(f'SET LOCAL ROLE "{pg_fixtures.APP_USER}"'))
            try:
                async with factory() as db:
                    yield db
            finally:
                await conn.execute(text("RESET ROLE"))

        monkeypatch.setattr(worker, "async_session", app_factory)
        monkeypatch.setattr(worker, "_utcnow", lambda: now)
        monkeypatch.setattr(worker, "_DISPATCHED_TASKS", {})
        monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 0)
        async with app_factory() as db:
            assert await db.scalar(select(RuntimeTask.id)) is None  # RLS really applies.
            role = (
                await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))
            ).one()
            assert tuple(role) == (False, False)
        assert await worker._has_due_runtime_work(worker_id="a2q", claim_tasks=True) is False
        yield factory, seed, now


async def test_idle_read_is_one_select_no_claims_and_keeps_warning(idle_db, owner_engine, caplog):
    statements = []

    def observe(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(owner_engine.sync_engine, "before_cursor_execute", observe)
    try:
        with caplog.at_level(logging.WARNING, logger="app.database"):
            assert await worker._has_due_runtime_work(worker_id="a2q", claim_tasks=True) is False
    finally:
        event.remove(owner_engine.sync_engine, "before_cursor_execute", observe)
    assert len([sql for sql in statements if sql.lstrip().startswith("SELECT")]) == 1
    assert not [sql for sql in statements if sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert [(record.levelname, record.args[0]) for record in caplog.records if "Entering BYPASS" in record.msg] == [
        ("WARNING", "runtime task worker discover due consumer work"),
    ]


@pytest.mark.parametrize("task_type", TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES)
@pytest.mark.parametrize("source", ("reconcile", "retry", "pending", "processing", "null-lease"))
async def test_combined_read_finds_every_terminal_lane_candidate(idle_db, task_type, source):
    factory, seed, now = idle_db
    async with factory() as db:
        task = RuntimeTask(
            id=uuid4(),
            tenant_id=seed.tenant_id,
            parent_agent_id=seed.agent_id,
            parent_session_id=str(seed.session_id),
            task_type=task_type,
            status="completed",
            created_at=now - timedelta(minutes=1),
            completion_outbox_settled_at=now,
        )
        db.add(task)
        await db.flush()
        if source == "retry":
            task.terminal_boundary_reconcile_attempted_at = now - timedelta(seconds=31)
        elif source in {"pending", "processing", "null-lease"}:
            task.terminal_boundary_enqueued_at = now
            db.add(
                RuntimeTerminalBoundaryOutbox(
                    id=uuid4(),
                    tenant_id=seed.tenant_id,
                    runtime_task_id=task.id,
                    task_type=task_type,
                    agent_id=seed.agent_id,
                    session_id=str(seed.session_id),
                    event_kind="turn_stop",
                    terminal_status="completed",
                    authority_ref="runtime_task",
                    authority_id=str(task.id),
                    binding_json={},
                    binding_sha256="a" * 64,
                    idempotency_key=uuid4().hex * 2,
                    status="pending" if source == "pending" else "processing",
                    available_at=now - timedelta(seconds=1),
                    lease_expires_at=None if source == "null-lease" else now - timedelta(seconds=1),
                )
            )
        await db.commit()
    # Trigger's existing discovery deliberately excludes NULL processing leases;
    # other lanes recover them. The shared predicate must preserve that rule.
    expected = not (task_type == "trigger" and source == "null-lease")
    assert await worker._has_due_runtime_work(worker_id="a2q") is expected


async def _input(factory, seed, now, *, dispatch_state="pending", intent="start_turn", namespace="human_input"):
    async with factory() as db:
        command = SessionCommand(
            id=uuid4(),
            tenant_id=seed.tenant_id,
            principal_type="user",
            principal_id=seed.user_id,
            session_id=seed.session_id,
            namespace=namespace,
            idempotency_key=uuid4().hex,
            command_kind=intent,
            request_hash="a" * 64,
            target_hash="b" * 64,
            request_json={},
            target_json={},
            receipt_ref="fixture",
            updated_at=now - timedelta(seconds=6),
        )
        db.add(command)
        await db.flush()
        turn = SessionTurnInput(
            id=uuid4(),
            tenant_id=seed.tenant_id,
            session_id=seed.session_id,
            command_id=command.id,
            intent=intent,
            content_hash="a" * 64,
            target_run_id=seed.task_id,
            target_turn_id=f"turn-{seed.task_id.hex}",
            terminal_fallback="queue_next_turn",
            queue_priority="next",
            queue_ordinal=1,
            status="queued",
        )
        db.add(turn)
        await db.flush()
        admission = SessionInputAdmission(
            id=uuid4(),
            tenant_id=seed.tenant_id,
            session_id=seed.session_id,
            command_id=command.id,
            input_id=turn.id,
            input_revision=1,
            state="admitted",
            hook_run_id=uuid4(),
            hook_idempotency_key=uuid4().hex,
            dispatch_state=dispatch_state,
            lease_expires_at=now - timedelta(seconds=1),
        )
        db.add(admission)
        await db.commit()
        return command, turn, admission


@pytest.mark.parametrize(
    "dispatch_state,intent",
    (("pending", "start_turn"), ("dispatching", "start_turn"), ("dispatched", "steer_current_turn")),
)
async def test_combined_read_finds_input_dispatch_and_terminal_rollover(idle_db, dispatch_state, intent):
    factory, seed, now = idle_db
    await _input(factory, seed, now, dispatch_state=dispatch_state, intent=intent)
    assert await worker._has_due_runtime_work(worker_id="a2q") is True


@pytest.mark.parametrize("kind", ("cancel-accepted", "cancel-applying", "replacement"))
async def test_combined_read_finds_control_and_replacement_recovery(idle_db, kind):
    factory, seed, now = idle_db
    command, turn, _admission = await _input(
        factory,
        seed,
        now,
        dispatch_state="not_applicable",
        intent="interrupt_and_replace",
        namespace="human_input" if kind == "replacement" else "control_input",
    )
    async with factory() as db:
        if kind == "replacement":
            saga_command = SessionCommand(
                id=uuid4(),
                tenant_id=seed.tenant_id,
                principal_type="user",
                principal_id=seed.user_id,
                session_id=seed.session_id,
                namespace="turn_replacement",
                causation_command_id=command.id,
                idempotency_key=uuid4().hex,
                command_kind="interrupt_and_replace",
                request_hash="a" * 64,
                target_hash="b" * 64,
                request_json={},
                target_json={},
                receipt_ref="fixture",
            )
            db.add(saga_command)
            await db.flush()
            db.add(
                SessionTurnReplacement(
                    id=uuid4(),
                    tenant_id=seed.tenant_id,
                    session_id=seed.session_id,
                    command_id=saga_command.id,
                    old_turn_id=f"turn-{seed.task_id.hex}",
                    old_run_id=seed.task_id,
                    replacement_turn_id="new",
                    replacement_input_id=turn.id,
                    state="requested",
                    lease_expires_at=now - timedelta(seconds=1),
                )
            )
        else:
            db.add(
                SessionControlInput(
                    id=uuid4(),
                    tenant_id=seed.tenant_id,
                    session_id=seed.session_id,
                    command_id=command.id,
                    expected_run_id=seed.task_id,
                    kind="cancel_run",
                    status=kind.removeprefix("cancel-"),
                    authority_snapshot_hash="a" * 64,
                    response_payload_hash="b" * 64,
                )
            )
        await db.commit()
    assert await worker._has_due_runtime_work(worker_id="a2q") is True


async def test_combined_read_finds_and_publishes_canonical_session_event(idle_db):
    from app.services.chat_transcript import append_session_event
    from app.services.session_event_outbox import SessionEventOutboxPublisher

    factory, seed, now = idle_db
    async with factory() as db:
        await append_session_event(
            db=db,
            tenant_id=seed.tenant_id,
            agent_id=seed.agent_id,
            user_id=seed.user_id,
            session_id=seed.session_id,
            actor_type="user",
            event_type="user_message",
            role="user",
            content="A2q",
            bridge_to_t0=False,
        )
        row = await db.scalar(select(SessionEventOutbox))
        row.available_at = now - timedelta(seconds=1)
        await db.commit()
    assert await worker._has_due_runtime_work(worker_id="a2q") is True
    published = []

    async def publish(payload):
        published.append(payload)

    result = await SessionEventOutboxPublisher(session_factory=worker.async_session).drain_once(
        worker_id="a2q", publish_callback=publish
    )
    assert result == {"claimed": 1, "published": 1, "retried": 0, "dead_lettered": 0}
    assert len(published) == 1
    assert await worker._has_due_runtime_work(worker_id="a2q") is False


@pytest.mark.parametrize("kind", ("channel", "budget"))
@pytest.mark.parametrize(
    "status,due", (("pending", True), ("pending", False), ("processing", True), ("processing", False))
)
async def test_combined_read_matches_delivery_retry_and_lease_clocks(idle_db, kind, status, due):
    factory, seed, now = idle_db
    moment = now - timedelta(seconds=61) if due else now + timedelta(seconds=61)
    async with factory() as db:
        if kind == "channel":
            row = ChannelDeliveryOutbox(
                id=uuid4(),
                tenant_id=seed.tenant_id,
                runtime_task_id=seed.task_id,
                agent_id=seed.agent_id,
                session_id=seed.session_id,
                channel="web",
                target_hash="a" * 64,
                terminal_status="completed",
                delivery_target_json={},
                text_content="A2q",
            )
        else:
            budget = RuntimeBudgetRun(
                id=uuid4(),
                tenant_id=seed.tenant_id,
                root_run_kind="fixture",
                root_run_key=uuid4().hex,
                root_agent_id=seed.agent_id,
                root_session_id=str(seed.session_id),
            )
            db.add(budget)
            await db.flush()
            budget_event = RuntimeBudgetEvent(
                id=uuid4(), tenant_id=seed.tenant_id, budget_run_id=budget.id, event_type="expired"
            )
            db.add(budget_event)
            await db.flush()
            row = BudgetTransitionOutbox(
                id=uuid4(),
                tenant_id=seed.tenant_id,
                budget_run_id=budget.id,
                budget_event_id=budget_event.id,
                transition="expired",
                session_id=seed.session_id,
                agent_id=seed.agent_id,
                content="A2q",
            )
        row.status, row.available_at, row.locked_at = status, moment, moment
        db.add(row)
        await db.commit()
    assert await worker._has_due_runtime_work(worker_id="a2q") is due


async def test_combined_read_finds_missing_budget_delivery_intent(idle_db):
    factory, seed, _now = idle_db
    async with factory() as db:
        budget = RuntimeBudgetRun(
            id=uuid4(),
            tenant_id=seed.tenant_id,
            root_run_kind="fixture",
            root_run_key=uuid4().hex,
            root_agent_id=seed.agent_id,
            root_session_id=str(seed.session_id),
        )
        db.add(budget)
        await db.flush()
        db.add(RuntimeBudgetEvent(id=uuid4(), tenant_id=seed.tenant_id, budget_run_id=budget.id, event_type="expired"))
        await db.commit()
    assert await worker._has_due_runtime_work(worker_id="a2q") is True


@pytest.mark.parametrize("shape", ("pending", "expired", "null-lease", "page-future-item"))
async def test_combined_read_finds_notification_and_page_reclaims(idle_db, shape):
    from app.services.runtime_notification_outbox import enqueue_completion_notification
    from tests.services.test_runtime_notification_outbox import _notification

    factory, seed, now = idle_db
    async with factory() as db:
        identity = await enqueue_completion_notification(
            db,
            _notification(
                tenant_id=seed.tenant_id, user_id=seed.user_id, agent_id=seed.agent_id, session_id=seed.session_id
            ),
        )
        row = await db.get(RuntimeNotificationOutbox, identity)
        row.available_at = now - timedelta(seconds=1)
        if shape in {"expired", "null-lease"}:
            row.integration_page_id = None
            row.status = "processing"
            row.lease_expires_at = now - timedelta(seconds=1) if shape == "expired" else None
        elif shape == "page-future-item":
            row.available_at = now + timedelta(days=1)
            page = RuntimeResultIntegrationPage(
                id=uuid4(),
                tenant_id=seed.tenant_id,
                parent_session_id=seed.session_id,
                parent_agent_id=seed.agent_id,
                parent_user_id=seed.user_id,
                root_scope_key="fixture",
                integration_epoch=1,
                delivery_mode="session_projection",
                mailbox_sequence_start=1,
                mailbox_sequence_end=1,
                item_count=1,
                manifest_json={},
                manifest_sha256="a" * 64,
                status="processing",
                lease_expires_at=now - timedelta(seconds=1),
            )
            db.add(page)
            await db.flush()
            row.integration_page_id = page.id
        await db.commit()
    assert await worker._has_due_runtime_work(worker_id="a2q") is True


async def test_combined_read_finds_missing_completion_intent(idle_db):
    factory, seed, _now = idle_db
    async with factory() as db:
        db.add(
            RuntimeTask(
                id=uuid4(),
                tenant_id=seed.tenant_id,
                parent_agent_id=seed.agent_id,
                parent_session_id=str(seed.session_id),
                task_type="subagent",
                status="completed",
            )
        )
        await db.commit()
    assert await worker._has_due_runtime_work(worker_id="a2q") is True


@pytest.mark.parametrize("status", ("pending", "resumable", "running"))
@pytest.mark.parametrize("due", (True, False))
async def test_combined_read_preserves_scheduled_claims_and_expired_lease_recovery(idle_db, status, due):
    factory, seed, now = idle_db
    async with factory() as db:
        db.add(
            RuntimeTask(
                id=uuid4(),
                tenant_id=seed.tenant_id,
                parent_agent_id=seed.agent_id,
                task_type="readiness_probe",
                status=status,
                scheduled_at=now - timedelta(seconds=1) if due else now + timedelta(days=1),
                claim_expires_at=now - timedelta(seconds=1),
            )
        )
        await db.commit()
    assert await worker._has_due_runtime_work(worker_id="a2q", claim_tasks=True) is due
    assert await worker._has_due_runtime_work(worker_id="a2q", claim_tasks=False) is False
