from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_execution_job(owner_sessionmaker, *, execution_status: str = "queued"):
    from app.models.runtime_task import RuntimeTask
    from tests.services.test_approval_ticket import _seed_approved_ticket

    tenant_id, user_id, agent_id, approval_id, _ = await _seed_approved_ticket(
        owner_sessionmaker,
        execution_status=execution_status,
    )
    task_id = uuid4()
    async with owner_sessionmaker() as db:
        from app.models.audit import ApprovalRequest

        approval = await db.get(ApprovalRequest, approval_id)
        assert approval is not None
        task = RuntimeTask(
            id=task_id,
            task_type="approval_execution",
            parent_agent_id=agent_id,
            tenant_id=tenant_id,
            status="running",
            claimed_by="approval-test-worker",
            claim_version=1,
            root_user_id=user_id,
            root_idempotency_key=f"approval-execution:{approval_id}",
            config_snapshot_hash="config",
            policy_snapshot_hash="policy",
            metadata_json={"schema": "approval_execution_job.v1", "approval_id": str(approval_id)},
        )
        db.add(task)
        await db.flush()
        approval.execution_task_id = task_id
        await db.commit()
    return tenant_id, user_id, agent_id, approval_id, task_id


@pytest.mark.asyncio
async def test_resolve_approval_commits_decision_and_job_in_one_real_postgres_transaction(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from sqlalchemy import select

    import app.database as database
    from app.models.audit import ApprovalRequest
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services.approval_service import ApprovalService
    from tests.services.test_approval_ticket import _seed_approved_ticket

    tenant_id, user_id, _, approval_id, _ = await _seed_approved_ticket(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        assert approval is not None
        approval.status = "pending"
        approval.execution_status = "pending"
        approval.resolved_at = None
        approval.resolved_by = None
        await db.commit()

    async def no_audit(*_args, **_kwargs):
        return None

    async def no_notification(*_args, **_kwargs):
        return None

    wakeups: list[str] = []

    async def capture_wakeup(*, reason, runtime_task_id):
        assert reason == "approval_execution_queued"
        wakeups.append(str(runtime_task_id))

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr("app.core.policy.write_audit_event", no_audit)
    monkeypatch.setattr("app.services.notification_service.send_notification", no_notification)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", capture_wakeup)

    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        assert user is not None
        resolved = await ApprovalService().resolve_approval(db, approval_id, user, "approve")
        execution_task_id = resolved.execution_task_id

    assert execution_task_id is not None
    assert wakeups == [str(execution_task_id)]
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        task = (
            await db.execute(
                select(RuntimeTask).where(RuntimeTask.root_idempotency_key == f"approval-execution:{approval_id}")
            )
        ).scalar_one()
        assert approval is not None
        assert approval.status == "approved"
        assert approval.execution_status == "queued"
        assert approval.execution_task_id == task.id == execution_task_id
        assert task.status == "pending"
        assert task.tenant_id == tenant_id


@pytest.mark.asyncio
async def test_approval_execution_job_consumes_once_and_recovers_terminal_without_replay(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.models.audit import ApprovalRequest
    from app.models.runtime_task import RuntimeTask
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    from app.services.approval_ticket import complete_approval_ticket, consume_approval_ticket

    tenant_id, user_id, agent_id, approval_id, task_id = await _seed_execution_job(owner_sessionmaker)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    calls: list[str] = []

    async def fake_execute_approved_tool(*, approval_id, expected_agent_id, approved_by_user_id):
        ticket = await consume_approval_ticket(
            approval_id=approval_id,
            expected_agent_id=expected_agent_id,
            expected_user_id=approved_by_user_id,
        )
        calls.append(ticket.idempotency_key)
        await complete_approval_ticket(
            approval_id=approval_id,
            tenant_id=ticket.tenant_id,
            status="succeeded",
            result="approved side effect",
            receipt={"status": "succeeded", "side_effect_state": "confirmed"},
        )
        return "approved side effect"

    monkeypatch.setattr("app.services.agent_tools.execute_approved_tool", fake_execute_approved_tool)

    assert await execute_claimed_approval_execution(task_id) == "succeeded"
    assert await execute_claimed_approval_execution(task_id) == "succeeded"
    assert calls == [f"approval:{approval_id}"]

    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        task = await db.get(RuntimeTask, task_id)
        assert approval is not None and task is not None
        assert approval.execution_status == "succeeded"
        assert task.status == "completed"
        assert task.metadata_json["outcome"]["status"] == "succeeded"
        assert task.completed_at is not None
        assert task.tenant_id == tenant_id
        assert task.parent_agent_id == agent_id
        assert task.root_user_id == user_id


@pytest.mark.asyncio
async def test_terminal_approval_atomically_enqueues_one_origin_continuation(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from sqlalchemy import func, select

    import app.database as database
    from app.models.audit import ApprovalRequest
    from app.models.chat_session import ChatSession
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_task import RuntimeTask
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    from app.services.approval_ticket import complete_approval_ticket, consume_approval_ticket

    tenant_id, user_id, agent_id, approval_id, task_id = await _seed_execution_job(owner_sessionmaker)
    session_id = uuid4()
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        task = await db.get(RuntimeTask, task_id)
        assert approval is not None and task is not None
        approval.details = {**dict(approval.details or {}), "session_id": str(session_id)}
        task.parent_session_id = str(session_id)
        task.root_session_id = str(session_id)
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                title="Approval origin",
                source_channel="web",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            )
        )
        await db.commit()
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    async def fake_execute_approved_tool(*, approval_id, expected_agent_id, approved_by_user_id):
        ticket = await consume_approval_ticket(
            approval_id=approval_id,
            expected_agent_id=expected_agent_id,
            expected_user_id=approved_by_user_id,
        )
        await complete_approval_ticket(
            approval_id=approval_id,
            tenant_id=ticket.tenant_id,
            status="succeeded",
            result="wrote workspace/report.md",
            receipt={"status": "succeeded", "side_effect_state": "confirmed"},
        )
        return "wrote workspace/report.md"

    monkeypatch.setattr("app.services.agent_tools.execute_approved_tool", fake_execute_approved_tool)

    assert await execute_claimed_approval_execution(task_id) == "succeeded"
    assert await execute_claimed_approval_execution(task_id) == "succeeded"

    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        rows = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.source_kind == "approval",
                        RuntimeNotificationOutbox.source_run_id == str(task_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        count = (
            await db.execute(
                select(func.count())
                .select_from(RuntimeNotificationOutbox)
                .where(
                    RuntimeNotificationOutbox.source_kind == "approval",
                    RuntimeNotificationOutbox.source_run_id == str(task_id),
                )
            )
        ).scalar_one()
    assert count == 1
    assert len(rows) == 1
    assert rows[0].parent_session_id == session_id
    assert rows[0].delivery_mode == "parent_continuation"
    assert rows[0].metadata_json["approval_id"] == str(approval_id)
    assert "wrote workspace/report.md" in rows[0].metadata_json["model_context"]
    assert approval is not None
    assert approval.execution_receipt["continuation_status"] == "queued"
    assert approval.execution_receipt["continuation_outbox_id"] == str(rows[0].id)


@pytest.mark.asyncio
async def test_reclaimed_executing_approval_is_quarantined_without_side_effect_replay(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.models.audit import ApprovalRequest
    from app.models.runtime_task import RuntimeTask
    from app.services.approval_execution_runtime import execute_claimed_approval_execution

    _, _, _, approval_id, task_id = await _seed_execution_job(
        owner_sessionmaker,
        execution_status="executing",
    )
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        assert approval is not None
        approval.consumed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        await db.commit()
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    async def forbidden_execute(**_kwargs):
        raise AssertionError("unknown side effects must never be replayed")

    monkeypatch.setattr("app.services.agent_tools.execute_approved_tool", forbidden_execute)

    assert await execute_claimed_approval_execution(task_id) == "needs_reconciliation"

    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        task = await db.get(RuntimeTask, task_id)
        assert approval is not None and task is not None
        assert approval.execution_status == "needs_reconciliation"
        assert approval.execution_receipt["automatic_replay"] is False
        assert task.status == "needs_reconciliation"
        assert task.metadata_json["reconciliation_retry_allowed"] is False
