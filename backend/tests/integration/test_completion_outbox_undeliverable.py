"""Permanent completion routes leave the recovery queue without notifying anyone."""

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import delete, event, func, select

from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import COMPLETION_OUTBOX_RETRY_SECONDS, TERMINAL_BOUNDARY_RECENT_WINDOW, RuntimeTask
from app.services import runtime_notification_outbox as outbox
from tests.services.test_a2a_continuation_completion_return import (
    _continuation_task,
    _drive_terminal_seam,
    _seed_a2a_parent_child,
)
from tests.services.test_runtime_notification_outbox import _clear_outbox, _seed_parent_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


class Clock(datetime):
    current = datetime.now(UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.current


@pytest.fixture
def clock(monkeypatch):
    Clock.current = datetime.now(UTC)
    monkeypatch.setattr(outbox, "datetime", Clock)
    return Clock


@pytest.fixture
async def seed(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    return await _seed_parent_session(owner_sessionmaker)


@pytest.fixture
async def owned_tasks(owner_sessionmaker):
    ids = []
    yield ids
    # These synthetic routes must not enter another test's terminal-boundary sweep.
    if ids:
        async with owner_sessionmaker() as db:
            await db.execute(delete(RuntimeTask).where(RuntimeTask.id.in_(ids)))
            await db.commit()


def terminal_task(seed, **overrides):
    tenant_id, user_id, agent_id, session_id = seed
    values = dict(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        task_type="trigger",
        status="failed",
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        child_session_id=str(session_id),
        completed_at=datetime.now(UTC) - TERMINAL_BOUNDARY_RECENT_WINDOW - timedelta(seconds=1),
        result_summary="Old result must not be delivered.",
        metadata_json={"user_id": str(user_id)},
    )
    return RuntimeTask(**(values | overrides))


@pytest.mark.parametrize(
    "reason",
    [
        "parent_session_id_invalid",
        "parent_agent_id_invalid",
        "parent_user_id_invalid",
        "parent_session_not_found",
        "child_session_not_found",
    ],
)
async def test_permanent_route_settles_once_without_delivery(
    seed, owner_sessionmaker, clock, monkeypatch, reason, owned_tasks
):
    task = terminal_task(seed)
    owned_tasks.append(task.id)
    async with owner_sessionmaker() as db:
        if reason == "parent_session_id_invalid":
            task.child_session_id = "legacy-trigger-session"
            task.completed_at = clock.current  # Invalid stored identifiers need no grace period.
        elif reason == "parent_agent_id_invalid":
            child_id = uuid.uuid4()
            db.add(
                ChatSession(
                    id=child_id,
                    tenant_id=seed[0],
                    agent_id=seed[2],
                    user_id=seed[1],
                    parent_session_id=seed[3],
                    peer_agent_id=None,
                )
            )
            task.task_type, task.parent_session_id = "a2a_continuation", str(child_id)
        elif reason == "parent_user_id_invalid":
            task.metadata_json = {"user_id": "legacy-owner", "keep": "original metadata"}
        elif reason == "parent_session_not_found":
            task.child_session_id = str(uuid.uuid4())
        else:
            task.task_type, task.parent_session_id = "a2a_continuation", str(uuid.uuid4())
        db.add(task)
        await db.commit()

    async def forbidden_enqueue(*args, **kwargs):
        pytest.fail("An undeliverable task must never create a member notification")

    monkeypatch.setattr(outbox, "enqueue_completion_notification", forbidden_enqueue)
    service = outbox.RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    first = await service.reconcile_terminal_tasks_once()
    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeTask, task.id)
        settled_at = stored.completion_outbox_settled_at
        attempts = stored.completion_outbox_attempt_count
        if reason == "parent_user_id_invalid":
            # A NULL session owner can be linked later; this reason keeps the existing retryable hold.
            assert first == 0 and settled_at is None and attempts == 1
            assert stored.completion_outbox_attempted_at == clock.current
            assert stored.completion_outbox_last_error == reason
    clock.current += timedelta(seconds=COMPLETION_OUTBOX_RETRY_SECONDS + 1)
    second = await service.reconcile_terminal_tasks_once()
    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeTask, task.id)
        assert await db.scalar(select(func.count()).select_from(RuntimeNotificationOutbox)) == 0
        assert (
            await db.scalar(
                select(func.count()).select_from(ChatTranscriptEvent).where(ChatTranscriptEvent.tenant_id == seed[0])
            )
            == 0
        )
    if reason == "parent_user_id_invalid":
        assert second == 0 and stored.completion_outbox_settled_at is None
        assert stored.completion_outbox_attempt_count == attempts + 1
        assert stored.completion_outbox_attempted_at == clock.current
        assert stored.completion_outbox_last_error == reason
        assert "completion_delivery_disposition" not in stored.metadata_json
    else:
        assert (first, second) == (1, 0)
        assert settled_at is not None and stored.completion_outbox_settled_at == settled_at
        assert stored.completion_outbox_attempt_count == attempts
        assert stored.completion_outbox_last_error is None
        assert stored.metadata_json["completion_delivery_disposition"] == f"undeliverable:{reason}"
    assert stored.metadata_json.get("keep") == task.metadata_json.get("keep")


async def test_a2a_continuation_unlinked_parent_owner_delivers_once_after_link(
    owner_sessionmaker, clock, monkeypatch, owned_tasks
):
    monkeypatch.setattr("app.services.runtime_budget_service.async_session", owner_sessionmaker)
    await _clear_outbox(owner_sessionmaker)
    (
        tenant_id,
        user_id,
        parent_agent_id,
        child_agent_id,
        parent_session_id,
        child_session_id,
    ) = await _seed_a2a_parent_child(owner_sessionmaker)
    task_id, task = _continuation_task(
        tenant_id=tenant_id,
        user_id=user_id,
        parent_agent_id=parent_agent_id,
        child_agent_id=child_agent_id,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        status="running",
    )
    owned_tasks.append(task_id)
    async with owner_sessionmaker() as db:
        parent = await db.get(ChatSession, parent_session_id)
        parent.user_id = None
        db.add(task)
        await db.commit()

    await _drive_terminal_seam(owner_sessionmaker, tenant_id=tenant_id, task_id=task_id)
    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeTask, task_id)
        assert stored.status == "completed"
        assert stored.completion_outbox_settled_at is None
        assert stored.completion_outbox_last_error == "parent_user_id_invalid"
        assert stored.completion_outbox_attempt_count == 1
        attempted_at = stored.completion_outbox_attempted_at
        assert attempted_at is not None
        assert await db.scalar(select(func.count()).select_from(RuntimeNotificationOutbox)) == 0

    service = outbox.RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.reconcile_terminal_tasks_once() == 0
    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeTask, task_id)
        assert stored.completion_outbox_attempt_count == 1
        parent = await db.get(ChatSession, parent_session_id)
        parent.user_id = user_id
        await db.commit()

    clock.current = attempted_at + timedelta(seconds=COMPLETION_OUTBOX_RETRY_SECONDS + 1)
    assert await service.reconcile_terminal_tasks_once() == 1
    delivered = await service.drain_once(worker_id="r15g-owner-linked")
    async with owner_sessionmaker() as db:
        parent_runs = list(
            await db.scalars(
                select(RuntimeTask.id).where(
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.parent_session_id == str(parent_session_id),
                )
            )
        )
        owned_tasks.extend(parent_runs)
        row = (
            await db.scalars(
                select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id))
            )
        ).one()
        assert delivered["delivered"] == 1, (delivered, row.last_error)
        assert row.status == "delivered" and row.delivered_at is not None
        assert row.parent_session_id == parent_session_id and row.parent_user_id == user_id
        assert len(parent_runs) == 1
        stored = await db.get(RuntimeTask, task_id)
        assert stored.completion_outbox_settled_at is not None
        assert stored.completion_outbox_last_error is None

    clock.current += timedelta(seconds=COMPLETION_OUTBOX_RETRY_SECONDS + 1)
    assert await service.reconcile_terminal_tasks_once() == 0
    assert (await service.drain_once(worker_id="r15g-owner-linked"))["delivered"] == 0
    async with owner_sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(RuntimeNotificationOutbox)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == parent_session_id,
                    ChatTranscriptEvent.event_type == "agent_task_notification",
                )
            )
            == 1
        )


@pytest.mark.parametrize("terminal_age", [None, timedelta(0), TERMINAL_BOUNDARY_RECENT_WINDOW])
async def test_missing_session_retries_until_existing_window_expires(
    seed, owner_sessionmaker, clock, terminal_age, owned_tasks
):
    task = terminal_task(
        seed,
        child_session_id=str(uuid.uuid4()),
        completed_at=None if terminal_age is None else clock.current - terminal_age,
    )
    owned_tasks.append(task.id)
    async with owner_sessionmaker() as db:
        db.add(task)
        await db.commit()
    service = outbox.RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.reconcile_terminal_tasks_once() == 0
    assert await service.reconcile_terminal_tasks_once() == 0  # The existing 30s backoff still applies.
    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeTask, task.id)
        assert stored.completion_outbox_attempt_count == 1
        assert stored.completion_outbox_settled_at is None
        assert stored.completion_outbox_last_error == "parent_session_not_found"
    clock.current += timedelta(seconds=COMPLETION_OUTBOX_RETRY_SECONDS + 1)
    repaired = await service.reconcile_terminal_tasks_once()
    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeTask, task.id)
    if terminal_age == TERMINAL_BOUNDARY_RECENT_WINDOW:
        assert repaired == 1 and stored.completion_outbox_settled_at is not None
    else:
        assert repaired == 0 and stored.completion_outbox_settled_at is None
        assert stored.completion_outbox_attempt_count == 2


async def test_target_session_committed_after_first_sweep_can_deliver(seed, owner_sessionmaker, clock, owned_tasks):
    target_id = uuid.uuid4()
    task = terminal_task(seed, child_session_id=str(target_id), completed_at=clock.current)
    owned_tasks.append(task.id)
    async with owner_sessionmaker() as db:
        db.add(task)
        await db.commit()
    service = outbox.RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.reconcile_terminal_tasks_once() == 0
    async with owner_sessionmaker() as db:
        db.add(ChatSession(id=target_id, tenant_id=seed[0], agent_id=seed[2], user_id=seed[1]))
        await db.commit()
    clock.current += timedelta(seconds=COMPLETION_OUTBOX_RETRY_SECONDS + 1)
    assert await service.reconcile_terminal_tasks_once() == 1
    async with owner_sessionmaker() as db:
        row = await db.scalar(
            select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task.id))
        )
        assert row is not None and row.parent_session_id == target_id


async def test_canonical_delegation_hold_keeps_retrying(seed, owner_sessionmaker, clock, owned_tasks):
    task = terminal_task(seed, task_type="delegation", terminal_boundary_generation=1)
    owned_tasks.append(task.id)
    async with owner_sessionmaker() as db:
        db.add(task)
        await db.commit()
    service = outbox.RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    for expected_attempts in (1, 2):
        assert await service.reconcile_terminal_tasks_once() == 0
        async with owner_sessionmaker() as db:
            stored = await db.get(RuntimeTask, task.id)
            assert stored.completion_outbox_attempt_count == expected_attempts
            assert stored.completion_outbox_last_error == "awaiting_canonical_delegation_projection"
            assert stored.completion_outbox_settled_at is None
            assert "completion_delivery_disposition" not in stored.metadata_json
        clock.current += timedelta(seconds=COMPLETION_OUTBOX_RETRY_SECONDS + 1)


async def test_production_shaped_backlog_drains_then_idle_is_one_query(
    owner_sessionmaker, owner_engine, clock, monkeypatch, owned_tasks
):
    await _clear_outbox(owner_sessionmaker)
    seeds = [await _seed_parent_session(owner_sessionmaker) for _ in range(3)]
    tasks = [
        terminal_task(
            seeds[index % 3],
            status="needs_reconciliation" if index < 2293 else "failed",
            child_session_id="legacy-trigger-session" if index < 2293 else str(uuid.uuid4()),
            created_at=clock.current - timedelta(days=90) + timedelta(seconds=index),
            completed_at=clock.current - timedelta(days=80),
            completion_outbox_attempt_count=53418 if index < 2293 else 50922,
            completion_outbox_attempted_at=clock.current - timedelta(minutes=5),
            completion_outbox_last_error="parent_session_id_invalid" if index < 2293 else "parent_session_not_found",
        )
        for index in range(2445)
    ]
    owned_tasks.extend(task.id for task in tasks)
    async with owner_sessionmaker() as db:
        db.add_all(tasks)
        await db.commit()

    async def forbidden_enqueue(*args, **kwargs):
        pytest.fail("The historical backlog must not notify members")

    monkeypatch.setattr(outbox, "enqueue_completion_notification", forbidden_enqueue)
    statements = []

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    service = outbox.RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    passes = []

    def statement_counts():
        # Report the unchanged RLS transaction framing separately from data work.
        rls = sum(statement.startswith("SET LOCAL app.current_tenant_id") for statement in statements)
        return len(statements) - rls, len(statements)

    event.listen(owner_engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        for _ in range(25):  # The existing sweep limit is 100.
            statements.clear()
            repaired = await service.reconcile_terminal_tasks_once()
            passes.append((repaired, *statement_counts()))
        # Advance past retry eligibility: BASE must revisit the backlog, HEAD must do no per-task work.
        clock.current += timedelta(seconds=COMPLETION_OUTBOX_RETRY_SECONDS + 1)
        statements.clear()
        idle_repaired = await service.reconcile_terminal_tasks_once()
        idle_statements, idle_total = statement_counts()
    finally:
        event.remove(owner_engine.sync_engine, "before_cursor_execute", count_statement)
    async with owner_sessionmaker() as db:
        stored = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.id.in_([task.id for task in tasks]))))
        pending = sum(task.completion_outbox_settled_at is None for task in stored)
        outbox_count = await db.scalar(select(func.count()).select_from(RuntimeNotificationOutbox))
        transcript_count = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(ChatTranscriptEvent.tenant_id.in_([seed[0] for seed in seeds]))
        )
    print(
        f"R15G_BACKLOG (repaired,data_sql,total_sql) passes={passes} "
        f"idle=({idle_repaired}, {idle_statements}, {idle_total}) "
        f"pending={pending} outbox={outbox_count} transcript={transcript_count}"
    )
    assert sum(count for count, _, _ in passes) == 2445
    assert pending == 0 and outbox_count == 0 and transcript_count == 0
    assert (idle_repaired, idle_statements) == (0, 1)
    assert all(task.metadata_json["completion_delivery_disposition"].startswith("undeliverable:") for task in stored)
