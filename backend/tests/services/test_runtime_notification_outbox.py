from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import delete, func, select, text

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services.runtime_notification_outbox import (
    CompletionNotification,
    RuntimeNotificationOutboxService,
    enqueue_completion_notification,
)


async def _seed_parent_session(owner_sessionmaker):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Outbox Tenant", slug=f"outbox-{tenant_id.hex[:12]}"))
        db.add(
            User(
                id=user_id,
                username=f"outbox-{user_id.hex[:10]}",
                email=f"{user_id.hex[:12]}@outbox.test",
                password_hash="x",
                display_name="Outbox Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Outbox Agent",
                role_description="deliver completion",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
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
        await db.commit()
    return tenant_id, user_id, agent_id, session_id


async def _clear_outbox(owner_sessionmaker) -> None:
    async with owner_sessionmaker() as db:
        await db.execute(delete(RuntimeNotificationOutbox))
        await db.commit()


def _notification(*, tenant_id, user_id, agent_id, session_id, source_run_id="run-1", status="completed"):
    return CompletionNotification(
        tenant_id=tenant_id,
        source_kind="subagent",
        source_run_id=source_run_id,
        parent_session_id=session_id,
        parent_agent_id=agent_id,
        parent_user_id=user_id,
        terminal_status=status,
        task_type="subagent",
        summary="The worker completed.",
        child_agent_name="Researcher",
        delivery_mode="session_projection",
        metadata={"evidence_ref": "t0:event-1"},
    )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_enqueue_is_deterministic_and_unique(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    notification = _notification(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        first = await enqueue_completion_notification(db, notification)
        second = await enqueue_completion_notification(db, notification)
        await db.commit()

    async with owner_sessionmaker() as db:
        count = (
            await db.execute(
                select(func.count())
                .select_from(RuntimeNotificationOutbox)
                .where(RuntimeNotificationOutbox.tenant_id == tenant_id)
            )
        ).scalar_one()

    assert first == second
    assert count == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_authoritative_enqueue_upgrades_reconciled_payload_by_rank(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    base = dict(
        tenant_id=tenant_id,
        source_kind="a2a_delegation",
        source_run_id="ranked-run",
        parent_session_id=session_id,
        parent_agent_id=agent_id,
        parent_user_id=user_id,
        terminal_status="completed",
        task_type="delegation",
        delivery_mode="parent_continuation",
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                **base,
                summary="generic result",
                payload_rank=10,
                metadata={"reconciled_from_terminal_runtime_task": True},
            ),
        )
        await db.commit()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await enqueue_completion_notification(
            db,
            CompletionNotification(
                **base,
                summary="rich result",
                payload_rank=100,
                artifacts=[{"path": "workspace/report.md"}],
                metadata={"artifact_contract": "fulfilled"},
            ),
        )
        await db.commit()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await enqueue_completion_notification(
            db,
            CompletionNotification(
                **base,
                summary="late generic result",
                payload_rank=10,
            ),
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == outbox_id))
        ).scalar_one()
    assert stored.summary == "rich result"
    assert stored.payload_rank == 100
    assert stored.artifacts_json == [{"path": "workspace/report.md"}]
    assert stored.metadata_json == {"artifact_contract": "fulfilled"}


@pytest.mark.usefixtures("migrated_pg_url")
async def test_claim_retry_and_terminal_ack_are_durable(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    notification = _notification(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_completion_notification(db, notification)
        await db.commit()

    attempts: list[uuid.UUID] = []

    async def flaky_deliver(item):
        attempts.append(item.id)
        if len(attempts) == 1:
            raise RuntimeError("temporary delivery failure")
        return {"status": "started", "runtime_task_id": "parent-run"}

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=3,
    )
    first = await service.drain_once(worker_id="worker-a", deliver=flaky_deliver)
    second = await service.drain_once(worker_id="worker-b", deliver=flaky_deliver)
    third = await service.drain_once(worker_id="worker-c", deliver=flaky_deliver)

    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == outbox_id))
        ).scalar_one()

    assert first == {"claimed": 1, "delivered": 0, "retried": 1, "deferred": 0, "dead_lettered": 0}
    assert second == {"claimed": 1, "delivered": 1, "retried": 0, "deferred": 0, "dead_lettered": 0}
    assert third == {"claimed": 0, "delivered": 0, "retried": 0, "deferred": 0, "dead_lettered": 0}
    assert attempts == [outbox_id, outbox_id]
    assert stored.status == "delivered"
    assert stored.attempt_count == 2
    assert stored.delivery_receipt_json["runtime_task_id"] == "parent-run"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_expired_processing_lease_is_reclaimed(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    notification = _notification(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_completion_notification(db, notification)
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker, lease_seconds=5)
    now = datetime.now(UTC)
    first = await service.claim_batch(worker_id="crashed-worker", now=now, limit=1)
    before_expiry = await service.claim_batch(worker_id="other-worker", now=now + timedelta(seconds=4), limit=1)
    reclaimed = await service.claim_batch(worker_id="other-worker", now=now + timedelta(seconds=6), limit=1)

    assert [item.id for item in first] == [outbox_id]
    assert before_expiry == []
    assert [item.id for item in reclaimed] == [outbox_id]
    assert reclaimed[0].attempt_count == 2


@pytest.mark.usefixtures("migrated_pg_url")
async def test_delivery_event_dedupes_when_ack_fails_after_commit(owner_sessionmaker, monkeypatch):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    notification = _notification(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_completion_notification(db, notification)
        await db.commit()

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=3,
    )
    original_mark_delivered = service._mark_delivered
    failed_once = False

    async def fail_first_ack(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("worker crashed before ack")
        return await original_mark_delivered(*args, **kwargs)

    monkeypatch.setattr(service, "_mark_delivered", fail_first_ack)
    first = await service.drain_once(worker_id="worker-a")
    second = await service.drain_once(worker_id="worker-b")

    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == outbox_id))
        ).scalar_one()
        events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.causation_id == outbox_id,
                        ChatTranscriptEvent.event_type == "agent_task_notification",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert first["retried"] == 1
    assert second["delivered"] == 1
    assert stored.status == "delivered"
    assert stored.delivery_receipt_json["deduplicated"] is True
    assert len(events) == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_outbox_rls_hides_other_tenant_rows(owner_sessionmaker, app_user_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    other_tenant_id, *_ = await _seed_parent_session(owner_sessionmaker)
    notification = _notification(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await enqueue_completion_notification(db, notification)
        await db.commit()

    async with tenant_scoped_session(other_tenant_id, session_factory=app_user_sessionmaker) as db:
        rls_context = (
            await db.execute(
                text(
                    "SELECT current_user, current_setting('app.current_tenant_id', true), "
                    "row_security_active('runtime_notification_outbox'), "
                    "(SELECT relrowsecurity FROM pg_class WHERE oid = 'runtime_notification_outbox'::regclass), "
                    "(SELECT relforcerowsecurity FROM pg_class WHERE oid = 'runtime_notification_outbox'::regclass), "
                    "pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid = 'runtime_notification_outbox'::regclass))"
                )
            )
        ).one()
        visible = list((await db.execute(select(RuntimeNotificationOutbox))).scalars().all())

    assert rls_context[0] == "rls_app_user"
    assert rls_context[1] == str(other_tenant_id)
    assert rls_context[2:5] == (True, True, True)
    assert visible == [], rls_context


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_backfills_terminal_runtime_task_missing_outbox(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="workflow",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                result_summary="workflow result",
                metadata_json={
                    "user_id": str(user_id),
                    "parent_session_id": str(session_id),
                    "artifacts": [{"type": "artifact", "path": "workspace/result.md"}],
                },
            )
        )
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    repaired = await service.reconcile_terminal_tasks_once(limit=10)

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id))
            )
        ).scalar_one()

    assert repaired == 1
    assert row.source_kind == "workflow"
    assert row.parent_session_id == session_id
    assert row.parent_user_id == user_id
    assert row.summary == "workflow result"
    assert row.artifacts_json == [{"type": "artifact", "path": "workspace/result.md"}]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_parent_continuation_waits_for_budget_approval_then_resumes(owner_sessionmaker):
    from app.services.runtime_budget_service import RuntimeBudgetRunCreate, RuntimeBudgetService

    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    budget_service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget_service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key="outbox-budget",
            source="web",
            profile="interactive",
            enforcement_mode="enforce",
            fail_mode="require_confirmation",
            max_continuation_wakes=0,
        )
    )
    notification = CompletionNotification(
        tenant_id=tenant_id,
        source_kind="subagent",
        source_run_id="budgeted-subagent",
        parent_session_id=session_id,
        parent_agent_id=agent_id,
        parent_user_id=user_id,
        terminal_status="completed",
        task_type="subagent",
        summary="budgeted result",
        delivery_mode="parent_continuation",
        metadata={"budget_run_id": str(budget_run.id)},
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_completion_notification(db, notification)
        await db.commit()

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        deferred_retry_seconds=0,
    )
    waiting = await service.drain_once(worker_id="worker-a")
    async with owner_sessionmaker() as db:
        pending = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == outbox_id))
        ).scalar_one()
    assert waiting["deferred"] == 1
    assert pending.status == "pending"
    assert pending.last_error == "runtime_budget_approval_required"

    await budget_service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=budget_run.id,
        reason="owner approved completion synthesis",
        actor_user_id=user_id,
        max_continuation_wakes=1,
    )
    resumed = await service.drain_once(worker_id="worker-b")

    async with owner_sessionmaker() as db:
        delivered = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == outbox_id))
        ).scalar_one()
        event_count = (
            await db.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.causation_id == outbox_id,
                    ChatTranscriptEvent.event_type == "agent_task_notification",
                )
            )
        ).scalar_one()
    assert resumed["delivered"] == 1
    assert delivered.status == "delivered"
    assert event_count == 1
