from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import delete, func, select, text, update

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.agent_team import AgentTeam, AgentTeamEvent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultIntegrationPage, RuntimeResultMailboxCursor, RuntimeResultObject
from app.models.runtime_task import RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.models.tenant import Tenant
from app.models.user import User
from app.services.runtime_notification_outbox import (
    CompletionNotification,
    RuntimeNotificationOutboxService,
    enqueue_completion_notification,
)
from app.services.runtime_result_store import decode_runtime_result_payload
from app.services.runtime_result_metrics import render_runtime_result_prometheus, reset_runtime_result_metrics


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
        await db.execute(delete(RuntimeResultIntegrationPage))
        await db.execute(delete(RuntimeResultMailboxCursor))
        await db.execute(delete(RuntimeResultObject))
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
async def test_same_source_payload_can_be_delivered_to_two_parent_mailboxes(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, first_session_id = await _seed_parent_session(owner_sessionmaker)
    second_session_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            ChatSession(
                id=second_session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                title="Second Parent Session",
                source_channel="web",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            )
        )
        await db.commit()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        first_id = await enqueue_completion_notification(
            db,
            _notification(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=first_session_id,
                source_run_id="shared-source-result",
            ),
        )
        second_id = await enqueue_completion_notification(
            db,
            _notification(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=second_session_id,
                source_run_id="shared-source-result",
            ),
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox)
                    .where(RuntimeNotificationOutbox.id.in_((first_id, second_id)))
                    .order_by(RuntimeNotificationOutbox.parent_session_id)
                )
            )
            .scalars()
            .all()
        )
        result_count = (
            await db.execute(
                select(func.count())
                .select_from(RuntimeResultObject)
                .where(RuntimeResultObject.source_run_id == "shared-source-result")
            )
        ).scalar_one()

    assert len(rows) == 2
    assert rows[0].result_object_id == rows[1].result_object_id
    assert rows[0].result_ref == rows[1].result_ref
    assert result_count == 1


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
        result_object = await db.get(RuntimeResultObject, stored.result_object_id)
        result_count = (
            await db.execute(
                select(func.count())
                .select_from(RuntimeResultObject)
                .where(
                    RuntimeResultObject.tenant_id == tenant_id,
                    RuntimeResultObject.source_kind == "a2a_delegation",
                    RuntimeResultObject.source_run_id == "ranked-run",
                )
            )
        ).scalar_one()
    assert result_object is not None
    payload = decode_runtime_result_payload(result_object.payload_bytes)
    assert payload["summary"] == "rich result"
    assert stored.payload_rank == 100
    assert payload["artifacts"] == [{"path": "workspace/report.md"}]
    assert payload["metadata"] == {"artifact_contract": "fulfilled"}
    assert stored.artifact_count == 1
    assert stored.metadata_json == {}
    assert result_count == 2


@pytest.mark.usefixtures("migrated_pg_url")
async def test_rejected_lower_rank_payload_cannot_overwrite_runtime_task_result_ref(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="authoritative result",
            )
        )
        await db.flush()
        base = dict(
            tenant_id=tenant_id,
            source_kind="subagent",
            source_run_id=str(task_id),
            parent_session_id=session_id,
            parent_agent_id=agent_id,
            parent_user_id=user_id,
            terminal_status="completed",
            task_type="subagent",
            delivery_mode="parent_continuation",
        )
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                **base,
                summary="authoritative result",
                metadata={"trace_id": "authoritative"},
                payload_rank=100,
            ),
        )
        await enqueue_completion_notification(
            db,
            CompletionNotification(
                **base,
                summary="late generic result",
                metadata={"trace_id": "late-generic"},
                payload_rank=10,
            ),
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeNotificationOutbox, outbox_id)
        task = await db.get(RuntimeTask, task_id)
        assert row is not None and task is not None
        result_object = await db.get(RuntimeResultObject, row.result_object_id)
        assert result_object is not None
        payload = decode_runtime_result_payload(result_object.payload_bytes)

    assert payload["summary"] == "authoritative result"
    assert row.metadata_json == {"trace_id": "authoritative"}
    assert task.metadata_json["runtime_result_ref"] == row.result_ref
    assert task.metadata_json["runtime_result_sha256"] == row.result_sha256
    assert row.result_ref in task.result_summary
    assert "late generic result" not in task.result_summary


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
async def test_artifact_trigger_notification_waits_for_delivered_projection(owner_sessionmaker):
    from app.services.runtime_terminal_boundary_outbox import enqueue_terminal_boundary

    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        task = RuntimeTask(
            id=task_id,
            tenant_id=tenant_id,
            task_type="trigger",
            status="needs_reconciliation",
            parent_agent_id=agent_id,
            child_session_id=str(session_id),
            result_summary="partial trigger result",
            metadata_json={"terminal_reason": "turn_stop"},
        )
        db.add(task)
        await db.flush()
        boundary = await enqueue_terminal_boundary(
            db,
            task=task,
            event_kind="turn_abort",
            agent_id=agent_id,
            session_id=session_id,
            terminal_status="needs_reconciliation",
            authority_ref="runtime_task",
            authority_id=task_id,
            binding={},
        )
        notification_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="trigger",
                source_run_id=task_id.hex,
                parent_session_id=session_id,
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status="needs_reconciliation",
                task_type="trigger",
                summary="partial trigger result",
                delivery_mode="session_projection",
                artifacts=[{"path": "runtime_artifacts/triggers/result.json"}],
            ),
        )
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.claim_batch(worker_id="notification-before-artifact", limit=10) == []

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await db.execute(
            update(RuntimeTerminalBoundaryOutbox)
            .where(RuntimeTerminalBoundaryOutbox.id == boundary.id)
            .values(status="delivered", delivered_at=datetime.now(UTC))
        )
        await db.commit()

    claimed = await service.claim_batch(worker_id="notification-after-artifact", limit=10)
    assert [item.id for item in claimed] == [notification_id]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_approval_continuation_retry_and_ack_update_approval_receipt(owner_sessionmaker):
    from app.models.audit import ApprovalRequest

    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    approval_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            ApprovalRequest(
                id=approval_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                action_type="workspace.write",
                status="approved",
                tool_name="write_file",
                execution_status="succeeded",
                execution_idempotency_key=f"approval:{approval_id}",
                execution_result="wrote report",
                execution_receipt={"status": "succeeded"},
                details={"session_id": str(session_id)},
            )
        )
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="approval",
                source_run_id=str(task_id),
                parent_session_id=session_id,
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status="completed",
                task_type="approval_execution",
                summary="wrote report",
                delivery_mode="parent_continuation",
                metadata={"approval_id": str(approval_id), "tool_name": "write_file"},
            ),
        )
        await db.commit()

    calls = 0

    async def flaky_delivery(_item):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary continuation failure")
        return {"status": "started", "runtime_task_id": "continued-run"}

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=3,
    )
    assert (await service.drain_once(worker_id="approval-worker-a", deliver=flaky_delivery))["retried"] == 1
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        assert approval is not None
        assert approval.execution_receipt["continuation_status"] == "retrying"
        assert "temporary continuation failure" in approval.execution_receipt["continuation_error"]

    assert (await service.drain_once(worker_id="approval-worker-b", deliver=flaky_delivery))["delivered"] == 1
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        row = await db.get(RuntimeNotificationOutbox, outbox_id)
        assert approval is not None and row is not None
        assert row.status == "delivered"
        assert approval.execution_receipt["continuation_status"] == "delivered"
        assert approval.execution_receipt["continuation_attempt_count"] == 2
        assert "continuation_error" not in approval.execution_receipt


@pytest.mark.usefixtures("migrated_pg_url")
async def test_dead_lettered_team_close_reopens_team_for_retry(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    team_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        team = AgentTeam(
            id=team_id,
            tenant_id=tenant_id,
            lead_agent_id=agent_id,
            parent_session_id=session_id,
            name="Research Team",
            status="closing",
            metadata_json={"close_attempt": 1},
        )
        db.add(team)
        await db.flush()
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="agent_team",
                source_run_id=f"agent_team_close:{team_id}:1",
                parent_session_id=str(session_id),
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status="completed",
                task_type="agent_team_close",
                summary="Synthesize Team results.",
                delivery_mode="parent_continuation",
                metadata={"agent_team_close_id": str(team_id)},
            ),
        )
        team.metadata_json = {**team.metadata_json, "close_notification_id": str(outbox_id)}
        await db.commit()

    async def fail_delivery(_item):
        raise RuntimeError("parent continuation unavailable")

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=1,
    )
    result = await service.drain_once(worker_id="worker-a", deliver=fail_delivery)

    async with owner_sessionmaker() as db:
        team = (await db.execute(select(AgentTeam).where(AgentTeam.id == team_id))).scalar_one()
        events = list(
            (await db.execute(select(AgentTeamEvent).where(AgentTeamEvent.team_id == team_id))).scalars().all()
        )

    assert result["dead_lettered"] == 1
    assert team.status == "active"
    assert team.metadata_json["close_synthesis_status"] == "delivery_failed"
    assert "parent continuation unavailable" in team.metadata_json["close_failure"]
    assert any(event.event_type == "team_close_delivery_failed" for event in events)


@pytest.mark.usefixtures("migrated_pg_url")
async def test_team_close_waits_for_idle_parent_before_lead_synthesis(owner_sessionmaker, monkeypatch):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    notification = CompletionNotification(
        tenant_id=tenant_id,
        source_kind="agent_team",
        source_run_id="agent_team_close:team-1:1",
        parent_session_id=session_id,
        parent_agent_id=agent_id,
        parent_user_id=user_id,
        terminal_status="completed",
        task_type="agent_team_close",
        summary="Synthesize Team results.",
        delivery_mode="parent_continuation",
        metadata={"agent_team_close_id": str(uuid.uuid4())},
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_completion_notification(db, notification)
        await db.commit()

    continuation_calls = []

    async def active_run(**_kwargs):
        return {"run_id": str(uuid.uuid4()), "status": "running"}

    async def unexpected_continuation(**kwargs):
        continuation_calls.append(kwargs)
        return {"status": "started"}

    monkeypatch.setattr("app.services.web_chat_runtime.get_active_web_chat_run", active_run)
    monkeypatch.setattr(
        "app.services.agent_session_continuation.continue_parent_session_with_result_page",
        unexpected_continuation,
    )
    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        deferred_retry_seconds=0,
    )

    result = await service.drain_once(worker_id="worker-a")

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == outbox_id))
        ).scalar_one()
    assert result["deferred"] == 1
    assert row.status == "pending"
    assert row.last_error == "parent_session_active"
    assert row.attempt_count == 0
    assert continuation_calls == []


@pytest.mark.usefixtures("migrated_pg_url")
async def test_expired_processing_lease_is_reclaimed(owner_sessionmaker):
    reset_runtime_result_metrics()
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
    metrics = render_runtime_result_prometheus()
    assert 'runtime_results_observed_total{source_kind="subagent"} 1' in metrics


@pytest.mark.usefixtures("migrated_pg_url")
async def test_prepared_page_retry_waits_until_row_available_at(owner_sessionmaker):
    reset_runtime_result_metrics()
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await enqueue_completion_notification(
            db,
            _notification(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                source_run_id="page-retry-delay",
            ),
        )
        await db.commit()

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=60,
        max_attempts=3,
    )
    claimed = await service.claim_batch(worker_id="retry-worker-a", limit=1)
    page = (await service.prepare_integration_pages(worker_id="retry-worker-a", claimed=claimed))[0]
    assert await service._mark_page_failed(
        page=page,
        worker_id="retry-worker-a",
        error=RuntimeError("provider unavailable"),
    ) == ("retry", 1)

    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeNotificationOutbox, claimed[0].id)
        assert row is not None
        available_at = row.available_at
    assert (
        await service.claim_batch(
            worker_id="retry-worker-early",
            now=available_at - timedelta(microseconds=1),
            limit=1,
        )
        == []
    )
    reclaimed = await service.claim_batch(
        worker_id="retry-worker-b",
        now=available_at + timedelta(microseconds=1),
        limit=1,
    )
    assert [item.id for item in reclaimed] == [claimed[0].id]
    metrics = render_runtime_result_prometheus()
    assert 'runtime_result_integration_pages_total{delivery_mode="session_projection",outcome="prepared"} 1' in metrics
    assert 'runtime_result_integration_pages_total{delivery_mode="session_projection",outcome="retry"} 1' in metrics


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
    original_mark_delivered = service._mark_page_delivered
    failed_once = False

    async def fail_first_ack(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("worker crashed before ack")
        return await original_mark_delivered(*args, **kwargs)

    monkeypatch.setattr(service, "_mark_page_delivered", fail_first_ack)
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
        result_object = await db.get(RuntimeResultObject, row.result_object_id)

    assert repaired >= 1
    assert row.source_kind == "workflow"
    assert row.parent_session_id == session_id
    assert row.parent_user_id == user_id
    assert result_object is not None
    payload = decode_runtime_result_payload(result_object.payload_bytes)
    assert payload["summary"] == "workflow result"
    assert payload["artifacts"] == [{"type": "artifact", "path": "workspace/result.md"}]
    assert row.artifact_count == 1

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        assert task.completion_outbox_generation == 1
        assert task.completion_outbox_settled_at is not None
        assert task.completion_outbox_last_error is None


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_ignores_historical_terminal_rows_without_generation(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    legacy_id = uuid.uuid4()
    current_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        for task_id, summary in ((legacy_id, "historical"), (current_id, "current")):
            db.add(
                RuntimeTask(
                    id=task_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="completed",
                    parent_agent_id=agent_id,
                    parent_session_id=str(session_id),
                    result_summary=summary,
                    metadata_json={
                        "user_id": str(user_id),
                        "parent_session_id": str(session_id),
                    },
                )
            )
        await db.flush()
        await db.execute(
            update(RuntimeTask).where(RuntimeTask.id == legacy_id).values(completion_outbox_generation=None)
        )
        await db.commit()

    repaired = await RuntimeNotificationOutboxService(session_factory=owner_sessionmaker).reconcile_terminal_tasks_once(
        limit=10
    )

    async with owner_sessionmaker() as db:
        source_ids = set(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox.source_run_id).where(
                        RuntimeNotificationOutbox.source_run_id.in_((str(legacy_id), str(current_id)))
                    )
                )
            )
            .scalars()
            .all()
        )
        legacy = await db.get(RuntimeTask, legacy_id)
        current = await db.get(RuntimeTask, current_id)

    assert repaired >= 1
    assert source_ids == {str(current_id)}
    assert legacy is not None and legacy.completion_outbox_generation is None
    assert current is not None and current.completion_outbox_settled_at is not None


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_settles_rollout_gap_when_outbox_already_exists(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        task = RuntimeTask(
            id=task_id,
            tenant_id=tenant_id,
            task_type="workflow",
            status="completed",
            parent_agent_id=agent_id,
            parent_session_id=str(session_id),
            result_summary="already durable",
            metadata_json={
                "user_id": str(user_id),
                "parent_session_id": str(session_id),
            },
        )
        db.add(task)
        await db.flush()
        await enqueue_completion_notification(
            db,
            _notification(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                source_run_id=str(task_id),
            ),
        )
        task.completion_outbox_settled_at = None
        await db.commit()

    repaired = await RuntimeNotificationOutboxService(session_factory=owner_sessionmaker).reconcile_terminal_tasks_once(
        limit=10
    )

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        outbox_count = await db.scalar(
            select(func.count(RuntimeNotificationOutbox.id)).where(
                RuntimeNotificationOutbox.source_run_id == str(task_id)
            )
        )

    assert repaired == 1
    assert outbox_count == 1
    assert task is not None and task.completion_outbox_settled_at is not None


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "needs_reconciliation"])
@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_settles_legacy_pure_workflow_trigger_without_notification(
    owner_sessionmaker,
    terminal_status,
):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, _user_id, agent_id, _session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="trigger",
                status=terminal_status,
                parent_agent_id=agent_id,
                result_summary="Workflow child owns result delivery.",
                metadata_json={"delivery": "workflow"},
            )
        )
        await db.commit()

    repaired = await RuntimeNotificationOutboxService(session_factory=owner_sessionmaker).reconcile_terminal_tasks_once(
        limit=10
    )

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        outbox_count = await db.scalar(
            select(func.count(RuntimeNotificationOutbox.id)).where(
                RuntimeNotificationOutbox.source_run_id == str(task_id)
            )
        )

    assert repaired == 1
    assert outbox_count == 0
    assert task is not None
    assert task.completion_outbox_settled_at is not None
    assert task.completion_outbox_last_error is None
    assert task.metadata_json["completion_delivery_disposition"] == "workflow_child_owned"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_holds_invalid_target_without_starving_next_task(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    invalid_id = uuid.uuid4()
    valid_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add_all(
            [
                RuntimeTask(
                    id=invalid_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="completed",
                    parent_agent_id=agent_id,
                    parent_session_id=str(uuid.uuid4()),
                    created_at=now - timedelta(seconds=2),
                    completed_at=now,
                    result_summary="target not ready",
                    metadata_json={"user_id": str(user_id)},
                ),
                RuntimeTask(
                    id=valid_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="completed",
                    parent_agent_id=agent_id,
                    parent_session_id=str(session_id),
                    created_at=now - timedelta(seconds=1),
                    completed_at=now - timedelta(seconds=1),
                    result_summary="deliver me",
                    metadata_json={
                        "user_id": str(user_id),
                        "parent_session_id": str(session_id),
                    },
                ),
            ]
        )
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.reconcile_terminal_tasks_once(limit=1) == 0
    assert await service.reconcile_terminal_tasks_once(limit=1) == 1

    async with owner_sessionmaker() as db:
        invalid = await db.get(RuntimeTask, invalid_id)
        delivered = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(valid_id))
            )
        ).scalar_one()

    assert delivered is not None
    assert invalid is not None
    assert invalid.completion_outbox_attempt_count == 1
    assert invalid.completion_outbox_attempted_at is not None
    assert invalid.completion_outbox_last_error == "parent_session_not_found"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_backfills_terminal_approval_continuation(owner_sessionmaker):
    await _clear_outbox(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="approval_execution",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="approved file result",
                metadata_json={"approval_id": str(approval_id), "tool_name": "write_file"},
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
    assert repaired >= 1
    assert row.source_kind == "approval"
    assert row.task_type == "approval_execution"
    assert row.parent_session_id == session_id
    assert row.parent_user_id == user_id
    assert row.metadata_json["approval_id"] == str(approval_id)


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
    waiting_run = await budget_service.get_run(tenant_id=tenant_id, budget_run_id=budget_run.id)
    assert waiting_run is not None and waiting_run.approval_episode_id is not None

    await budget_service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=budget_run.id,
        reason="owner approved completion synthesis",
        approval_episode_id=waiting_run.approval_episode_id,
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
    assert resumed["delivered"] == 1, (resumed, delivered.last_error)
    assert delivered.status == "delivered"
    assert event_count == 1
