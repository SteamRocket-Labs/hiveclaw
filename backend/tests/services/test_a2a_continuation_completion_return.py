"""Real-PG regression for DAY1-A2A-CONT-RETURN-001.

A follow-up sent to an existing A2A delegation child session creates an
executable ``a2a_continuation`` successor. Its terminal completion must create
exactly one durable parent completion notification through the existing
completion-outbox contract (reconcile sweep -> mailbox -> automatic parent
wake), while an ordinary top-level ``web_chat_turn`` stays ineligible and can
never self-notify.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import delete, func, select, update

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultIntegrationPage, RuntimeResultMailboxCursor, RuntimeResultObject
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService


async def _seed_a2a_parent_child(owner_sessionmaker):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    child_agent_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="A2A Cont Tenant", slug=f"a2a-cont-{tenant_id.hex[:12]}"))
        db.add(
            User(
                id=user_id,
                username=f"a2a-cont-{user_id.hex[:10]}",
                email=f"{user_id.hex[:12]}@a2a-cont.test",
                password_hash="x",
                display_name="A2A Cont Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=parent_agent_id,
                tenant_id=tenant_id,
                name="Parent Agent A",
                role_description="delegates work",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        db.add(
            Agent(
                id=child_agent_id,
                tenant_id=tenant_id,
                name="Worker Agent B",
                role_description="receives follow-ups",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=parent_session_id,
                tenant_id=tenant_id,
                agent_id=parent_agent_id,
                user_id=user_id,
                title="Parent Session",
                source_channel="web",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            )
        )
        db.add(
            ChatSession(
                id=child_session_id,
                tenant_id=tenant_id,
                agent_id=child_agent_id,
                user_id=user_id,
                title="Delegation Child Session",
                source_channel="agent",
                session_kind="delegation_run",
                actor_type="agent",
                runtime_source="delegation",
                peer_agent_id=parent_agent_id,
                parent_session_id=parent_session_id,
                root_session_id=parent_session_id,
                visibility_scope="team",
                listed_surface="parent",
            )
        )
        await db.commit()
    return tenant_id, user_id, parent_agent_id, child_agent_id, parent_session_id, child_session_id


async def _clear_outbox(owner_sessionmaker) -> None:
    async with owner_sessionmaker() as db:
        await db.execute(delete(RuntimeNotificationOutbox))
        await db.execute(delete(RuntimeResultIntegrationPage))
        await db.execute(delete(RuntimeResultMailboxCursor))
        await db.execute(delete(RuntimeResultObject))
        await db.commit()


def _continuation_task(
    *,
    tenant_id,
    user_id,
    parent_agent_id,
    child_agent_id,
    parent_session_id,
    child_session_id,
    task_type="a2a_continuation",
    parent_agent_metadata=True,
):
    task_id = uuid.uuid4()
    metadata = {
        "user_id": str(user_id),
        "session_id": str(child_session_id),
        "parent_session_id": str(parent_session_id),
        "source": "agent_session_mailbox",
        "agent_session_message": True,
    }
    if parent_agent_metadata:
        metadata["parent_agent_id"] = str(parent_agent_id)
    task = RuntimeTask(
        id=task_id,
        tenant_id=tenant_id,
        task_type=task_type,
        status="completed",
        parent_agent_id=child_agent_id,
        child_agent_id=child_agent_id,
        child_agent_name="Worker Agent B",
        parent_session_id=str(child_session_id),
        child_session_id=str(child_session_id),
        root_user_id=user_id,
        root_session_id=str(parent_session_id),
        root_runtime_task_id=task_id,
        prompt="compute 31*37 and reply with the marker",
        result_summary="A2A-CONT-P1-B-FOLLOW-947 31*37=1147 Worker Agent B",
        metadata_json=metadata,
    )
    return task_id, task


@pytest.mark.usefixtures("migrated_pg_url")
async def test_terminal_a2a_continuation_reconciles_exactly_one_parent_completion(owner_sessionmaker):
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
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(task)
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    repaired = await service.reconcile_terminal_tasks_once(limit=10)

    async with owner_sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id))
                )
            )
            .scalars()
            .all()
        )

    assert repaired == 1
    assert len(rows) == 1
    row = rows[0]
    assert row.source_kind == "a2a_continuation"
    assert row.task_type == "a2a_continuation"
    assert row.tenant_id == tenant_id
    assert row.parent_session_id == parent_session_id
    assert row.parent_agent_id == parent_agent_id
    assert row.parent_user_id == user_id
    assert row.child_session_id == child_session_id
    assert row.child_agent_name == "Worker Agent B"
    assert row.terminal_status == "completed"
    assert row.delivery_mode == "parent_continuation"
    assert row.result_ref

    # Replay/idempotency: a second sweep must not create a second notification.
    again = await service.reconcile_terminal_tasks_once(limit=10)
    async with owner_sessionmaker() as db:
        count = (
            await db.execute(
                select(func.count())
                .select_from(RuntimeNotificationOutbox)
                .where(RuntimeNotificationOutbox.source_run_id == str(task_id))
            )
        ).scalar_one()
        settled_task = await db.get(RuntimeTask, task_id)
    assert again == 0
    assert count == 1
    assert settled_task is not None
    assert settled_task.completion_outbox_generation == 1
    assert settled_task.completion_outbox_settled_at is not None
    assert settled_task.completion_outbox_last_error is None
    assert settled_task.metadata_json["runtime_result_ref"] == row.result_ref


@pytest.mark.usefixtures("migrated_pg_url")
async def test_terminal_web_chat_turn_is_never_completion_outbox_eligible(owner_sessionmaker):
    """An ordinary web_chat_turn with identical metadata can never self-notify."""

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
        task_type="web_chat_turn",
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(task)
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    await service.reconcile_terminal_tasks_once(limit=10)
    await service.reconcile_terminal_tasks_once(limit=10)

    async with owner_sessionmaker() as db:
        outbox_count = (
            await db.execute(
                select(func.count())
                .select_from(RuntimeNotificationOutbox)
                .where(RuntimeNotificationOutbox.source_run_id == str(task_id))
            )
        ).scalar_one()
        stored = await db.get(RuntimeTask, task_id)
    assert outbox_count == 0
    assert stored is not None
    assert stored.completion_outbox_attempted_at is None
    assert stored.completion_outbox_settled_at is None


@pytest.mark.usefixtures("migrated_pg_url")
async def test_a2a_continuation_hold_is_typed_observable_and_recovers(owner_sessionmaker):
    """A missing structured parent binding holds with a typed reason, backs off,
    and re-enters the normal path once the durable binding is present."""

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
        parent_agent_metadata=False,
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(task)
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    held = await service.reconcile_terminal_tasks_once(limit=10)

    async with owner_sessionmaker() as db:
        outbox_count = (
            await db.execute(
                select(func.count())
                .select_from(RuntimeNotificationOutbox)
                .where(RuntimeNotificationOutbox.source_run_id == str(task_id))
            )
        ).scalar_one()
        stored = await db.get(RuntimeTask, task_id)
    assert held == 0
    assert outbox_count == 0
    assert stored is not None
    assert stored.completion_outbox_last_error == "parent_session_not_found"
    assert stored.completion_outbox_attempt_count == 1
    assert stored.completion_outbox_settled_at is None

    # The 30s retry backoff holds the row out of the immediate next sweep.
    skipped = await service.reconcile_terminal_tasks_once(limit=10)
    async with owner_sessionmaker() as db:
        stored = await db.get(RuntimeTask, task_id)
    assert skipped == 0
    assert stored is not None and stored.completion_outbox_attempt_count == 1

    # Recovery: once the structured parent binding is durable and the backoff
    # window passed, the normal reconcile path enqueues exactly one row.
    async with owner_sessionmaker() as db:
        await db.execute(
            update(RuntimeTask)
            .where(RuntimeTask.id == task_id)
            .values(
                metadata_json={
                    **dict(stored.metadata_json or {}),
                    "parent_agent_id": str(parent_agent_id),
                },
                completion_outbox_attempted_at=datetime.now(UTC) - timedelta(seconds=31),
            )
        )
        await db.commit()

    repaired = await service.reconcile_terminal_tasks_once(limit=10)
    async with owner_sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id))
                )
            )
            .scalars()
            .all()
        )
        stored = await db.get(RuntimeTask, task_id)
    assert repaired == 1
    assert len(rows) == 1
    assert rows[0].parent_agent_id == parent_agent_id
    assert rows[0].parent_session_id == parent_session_id
    assert stored is not None
    assert stored.completion_outbox_settled_at is not None
    assert stored.completion_outbox_last_error is None


@pytest.mark.usefixtures("migrated_pg_url")
async def test_a2a_continuation_completion_delivers_exactly_one_parent_wake(owner_sessionmaker, monkeypatch):
    """End-to-end: terminal continuation -> outbox -> delivered page -> the
    existing automatic parent wake (a real parent web_chat_turn run), with no
    polling and no self-notification loop."""

    # ``start_web_chat_run`` creates the parent wake run's budget root through
    # ``RuntimeBudgetService()``'s process-global default factory (correct in
    # production). Bind that factory to the migrated Testcontainers database,
    # mirroring tests/services/test_session_input_control_v2.py.
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
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(task)
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.reconcile_terminal_tasks_once(limit=10) == 1
    counts = await service.drain_once(worker_id="a2a-continuation-worker")

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id))
            )
        ).scalar_one()
        wake_events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == parent_session_id,
                        ChatTranscriptEvent.event_type == "agent_task_notification",
                    )
                )
            )
            .scalars()
            .all()
        )
        parent_runs = list(
            (
                await db.execute(
                    select(RuntimeTask).where(
                        RuntimeTask.tenant_id == tenant_id,
                        RuntimeTask.parent_session_id == str(parent_session_id),
                        RuntimeTask.id != task_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert counts["delivered"] == 1, (counts, row.last_error)
    assert row.status == "delivered"
    assert row.delivered_at is not None
    assert len(wake_events) == 1
    assert len(parent_runs) == 1
    parent_run = parent_runs[0]
    assert parent_run.task_type == "web_chat_turn"
    assert parent_run.parent_agent_id == parent_agent_id
    assert parent_run.status in {"pending", "running"}

    # The parent wake run is an ordinary web_chat_turn: it must not become a
    # new completion-outbox source (no self-notification loop).
    assert await service.reconcile_terminal_tasks_once(limit=10) == 0
    async with owner_sessionmaker() as db:
        loop_count = (
            await db.execute(
                select(func.count())
                .select_from(RuntimeNotificationOutbox)
                .where(RuntimeNotificationOutbox.source_run_id == str(parent_run.id))
            )
        ).scalar_one()
    assert loop_count == 0

    # Delivery replay is deduplicated: no second event, no second parent run.
    replay = await service.drain_once(worker_id="a2a-continuation-worker")
    async with owner_sessionmaker() as db:
        event_count = (
            await db.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == parent_session_id,
                    ChatTranscriptEvent.event_type == "agent_task_notification",
                )
            )
        ).scalar_one()
        parent_run_count = (
            await db.execute(
                select(func.count())
                .select_from(RuntimeTask)
                .where(
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.parent_session_id == str(parent_session_id),
                    RuntimeTask.id != task_id,
                )
            )
        ).scalar_one()
    assert replay["delivered"] == 0
    assert event_count == 1
    assert parent_run_count == 1
