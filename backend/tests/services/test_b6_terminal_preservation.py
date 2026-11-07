"""Settled run authority survives model cancellation and late finalizers."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select

from app.models.runtime_task import RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.models.session_v2 import SessionModelResult
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _prepare(db, common, provider_options):
    from app.services.session_model_round import bind_round_inputs, prepare_model_request

    messages = await bind_round_inputs(db, **common)
    request_id = await prepare_model_request(db, **common, messages=messages, tools=[],
        provider="anthropic", model="local", wire_request={"messages": messages, "tools": [],
            "reasoning": provider_options})
    await db.commit()
    return request_id


async def _deliver_once(factory, tenant_id, run_id, validator):
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    service = RuntimeTerminalBoundaryOutboxService(session_factory=factory)
    calls = []

    async def deliver(item):
        calls.append(item.runtime_task_id)
        return {"source_ref": f"runtime-terminal-boundary://{item.id}"}

    first = await service.drain_once(tenant_id=tenant_id, worker_id="b6-terminal-test",
        canonical_validator=validator, process_callback=deliver)
    second = await service.drain_once(tenant_id=tenant_id, worker_id="b6-terminal-test",
        canonical_validator=validator, process_callback=deliver)
    assert first["delivered"] == 1 and second["claimed"] == 0
    assert calls == [run_id]
    async with factory() as db:
        rows = list(await db.scalars(select(RuntimeTerminalBoundaryOutbox).where(
            RuntimeTerminalBoundaryOutbox.runtime_task_id == run_id)))
        assert len(rows) == 1 and rows[0].status == "delivered"


@pytest.mark.parametrize("provider_options", [{}, {"mcp_servers": [{"name": "local"}]}])
async def test_revoked_model_call_seals_round_without_rewriting_terminal_run(owner_sessionmaker, provider_options):
    from app.models.user import User
    from app.services.session_model_round import fail_model_request
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.user_offboarding_service import revoke_user_authority
    from app.services.web_terminal_boundary_processor import validate_web_terminal_boundary

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    common = dict(tenant_id=tenant_id, agent_id=agent_id, session_id=session_id, run_id=run_id,
        turn_id=f"turn-{run_id.hex}", round_index=1)
    async with owner_sessionmaker() as db:
        request_id = await _prepare(db, common, provider_options)
        user = await db.get(User, user_id)
        await revoke_user_authority(db, target_user=user, actor_user=user, now=datetime.now(timezone.utc))
        await db.commit()
        run = await db.get(RuntimeTask, run_id)
        before = (run.status, dict(run.metadata_json), run.claim_version, run.completed_at, run.result_summary)
        assert run.status == "needs_reconciliation"
        assert run.budget_terminal_reason == "root_user_offboarded"
    async with owner_sessionmaker() as db:
        token = set_runtime_task_fence(task_id=run_id, claim_version=0, worker_id="revoked-web-worker")
        try:
            await fail_model_request(db, **common, provider_request_id=request_id,
                error_class="cancelled", retry_safe=False)
            await db.commit()
        finally:
            reset_runtime_task_fence(token)
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == run_id))
        assert (run.status, run.metadata_json, run.claim_version, run.completed_at, run.result_summary) == before
        assert result.state == ("needs_reconciliation" if provider_options else "failed")
        assert result.seal_json["retry_safe"] is False
    await _deliver_once(owner_sessionmaker, tenant_id, run_id, validate_web_terminal_boundary)


@pytest.mark.parametrize("provider_options,expected", [({}, "killed"),
    ({"mcp_servers": [{"name": "local"}]}, "needs_reconciliation")])
async def test_business_finalizer_keeps_round_terminal_and_delivers_boundary_once(
    owner_sessionmaker, monkeypatch, provider_options, expected,
):
    import app.database as database
    from app.models.task import Task, TaskLog
    from app.services.business_task_runtime import (
        TaskExecutionOutcome, TaskExecutionStatus, finalize_business_task_execution,
    )
    from app.services.direct_invocation_terminal_boundary_processor import validate_direct_terminal_boundary
    from app.services.session_model_round import fail_model_request
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    task_id = uuid.uuid4()
    common = dict(tenant_id=tenant_id, agent_id=agent_id, session_id=session_id, run_id=run_id,
        turn_id=f"turn-{run_id.hex}", round_index=1)
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        db.add(Task(id=task_id, tenant_id=tenant_id, agent_id=agent_id, title="Local work",
            created_by=user_id, responsible_user_id=user_id, root_session_id=session_id,
            request_id=str(uuid.uuid4()), request_hash="a" * 64,
            active_runtime_task_id=run_id, status="doing", last_execution_status="running"))
        run.task_type = "business_task"
        run.claimed_by, run.claim_version = "b6-worker", 7
        run.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        run.metadata_json = {**run.metadata_json, "business_task_id": str(task_id),
            "requester_user_id": str(user_id), "phase": "invoking"}
        await db.commit()
        request_id = await _prepare(db, common, provider_options)
        await fail_model_request(db, **common, provider_request_id=request_id,
            error_class="cancelled", retry_safe=False)
        await db.commit()
        await db.refresh(run)
        before = (run.status, run.metadata_json.get("terminal_reason"), run.claim_version, run.completed_at)
        assert run.status == expected
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    outcome = TaskExecutionOutcome(status=TaskExecutionStatus.CANCELLED, summary="cancelled by user")
    token = set_runtime_task_fence(task_id=run_id, claim_version=7, worker_id="b6-worker")
    try:
        for _ in range(2):
            receipt = await finalize_business_task_execution(runtime_task_id=run_id, outcome=outcome)
            assert receipt.terminal_status == expected and receipt.terminal_transitioned is False
    finally:
        reset_runtime_task_fence(token)
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        task = await db.get(Task, task_id)
        assert (run.status, run.metadata_json.get("terminal_reason"), run.claim_version, run.completed_at) == before
        assert task.status == ("cancelled" if expected == "killed" else expected)
        assert task.last_execution_status == ("cancelled" if expected == "killed" else expected)
        logs = list(await db.scalars(select(TaskLog).where(TaskLog.task_id == task_id)))
        assert len(logs) == 1 and run.result_summary in logs[0].content
    await _deliver_once(owner_sessionmaker, tenant_id, run_id, validate_direct_terminal_boundary)


@pytest.mark.parametrize("approval_status,persisted,expected", [
    ("pending", "succeeded", "waiting_user"),
    ("approved", "succeeded", "succeeded"),
    ("approved", "waiting_user", "succeeded"),
    ("rejected", "waiting_user", "cancelled"),
    ("pending", "completed", "succeeded"),
    ("approved", "cancelled", "cancelled"),
    ("pending", "failed", "failed"),
])
async def test_operator_transcript_reads_current_approval_decision(
    owner_sessionmaker, monkeypatch, approval_status, persisted, expected,
):
    from app.api import chat_sessions
    from app.models.agent import Agent
    from app.models.audit import ApprovalRequest
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.user import User

    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    approval_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(ApprovalRequest(id=approval_id, tenant_id=tenant_id, agent_id=agent_id,
            requested_by=user_id, action_type="tool", status=approval_status))
        db.add(ChatTranscriptEvent(tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
            sequence=1, event_type="permission_request", item_type="approval_request", item_status=persisted,
            actor_type="system", visibility_scope="direct_user", listed_surface="chat",
            content="Local approval", metadata_json={"status": "approval_required", "approval_id": str(approval_id)}))
        await db.commit()

        async def reachable(*_args):
            return await db.get(Agent, agent_id), "manage"

        async def authorized(**_kwargs):
            return "operator_inspect_grant"

        monkeypatch.setattr(chat_sessions, "check_agent_operator_reachability", reachable)
        monkeypatch.setattr(chat_sessions, "_authorize_loaded_session", authorized)
        user = await db.get(User, user_id)
        items = await chat_sessions.get_session_transcript(agent_id=agent_id, session_id=session_id,
            operator_view=True, operator_reason="Local test", current_user=user, db=db)
        assert len(items) == 1 and items[0]["item_status"] == expected
        stored = await db.get(ChatTranscriptEvent, uuid.UUID(items[0]["id"]))
        assert stored.item_status == persisted and stored.metadata_json["status"] == "approval_required"
