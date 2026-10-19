"""Enterprise tickets remain distinct from local permissions in the originating work."""

import json
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db, pin_rls_tenant_context
from app.models.audit import ApprovalRequest
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.guard_policy import GuardPolicy
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from tests.services.test_session_input_control_v2 import _mark_web_terminal_boundary_delivered, _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("tool_name,action", [("read_file", "approve"), ("read_file", "reject"),
    ("read_file", "view_only"), ("feishu_doc_create", "reject"), ("feishu_doc_create", "view_only")])
async def test_actual_enterprise_ticket_is_not_a_local_permission_and_stays_in_its_work(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tool_name, action,
):
    from app.api.chat_sessions import router
    from app.core.execution_context import ExecutionIdentity
    from app.core.security import get_current_user
    from app.services import agent_tools
    from app.services.session_model_round import prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import prepare_tool_invocation, complete_tool_invocation

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    for module in ["app.database", "app.tools.resolver", "app.tools.governance_resolver"]:
        monkeypatch.setattr(f"{module}.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)

    async def no_wakeup(**_kwargs):
        pass

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    from app.config import get_settings
    from pathlib import Path

    workspace = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "requested.md").write_text("Synthetic approved source.")
    async with owner_sessionmaker() as db:
        from app.services.workspace_resource_authority import register_workspace_path

        await register_workspace_path(db, tenant_id=tenant_id, agent_id=agent_id, path="workspace/requested.md",
            owner_user_id=user_id, root_session_id=session_id, source="synthetic-test-fixture")
        db.add(GuardPolicy(tenant_id=tenant_id, zone_guard={"tool_rules": [{"tools": [tool_name], "decision": "require_approval"}]}))
        if tool_name == "feishu_doc_create":
            from app.models.tenant_channel_config import TenantChannelConfig
            from app.models.capability_policy import CapabilityPolicy
            db.add(TenantChannelConfig(tenant_id=tenant_id, channel_type="feishu", app_id="synthetic-approval-app",
                app_secret="synthetic-approval-secret", is_active=True))
            db.add(CapabilityPolicy(tenant_id=tenant_id, capability="channel.feishu.document", allowed=True,
                requires_approval=False))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        args, call_id, turn_id = ({"path": "workspace/requested.md"} if tool_name == "read_file" else {"title": "Requested report"}), f"call-{run_id.hex}", f"turn-{run_id.hex}"
        wire = {"messages": [{"role": "user", "content": "Read the requested file"}],
                "tools": [{"type": "function", "function": {"name": tool_name}}], "max_tokens": 8192}
        request_id = await prepare_model_request(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
            run_id=run_id, turn_id=turn_id, round_index=1, continuation_index=0, messages=wire["messages"], tools=wire["tools"],
            provider="openai", model="synthetic-no-network", wire_request=wire, attempt_owner="enterprise-test")
        await seal_model_response(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id, run_id=run_id,
            turn_id=turn_id, round_index=1, provider_request_id=request_id, response={"content": None,
                "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(args)}}],
                "finish_reason": "tool_calls", "usage": {"prompt_tokens": 20, "completion_tokens": 5}})
        await commit_sealed_model_round(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id, run_id=run_id,
            turn_id=turn_id, round_index=1, provider_request_id=request_id)
        invocation = await prepare_tool_invocation(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
            run_id=run_id, provider_request_id=request_id, provider_tool_use_id=call_id, tool_name=tool_name, arguments=args)
        await db.commit()
        trace = {}
        outcome = await agent_tools._get_tool_runtime_service().execute(tool_name, args, agent_id=agent_id,
            user_id=user_id, session_id=str(session_id), runtime_task_id=str(run_id), turn_id=turn_id,
            execution_identity=ExecutionIdentity("delegated_user", user_id, "Synthetic requester"),
            tool_call_id=call_id, emit_runtime_hooks=False, trace_metadata_sink=trace)
        payload = json.loads(str(outcome))
        assert payload["status"] == "approval_required", outcome
        from app.services.web_chat_runtime import _interactive_pause_summary_for_tool_call

        assert _interactive_pause_summary_for_tool_call({"status": "done", "result": str(outcome),
            "tool_execution_evidence": _execution_evidence(trace)}) == "awaiting_company_approval"
        approval_id = uuid.UUID(payload["approval_id"])
        events = await complete_tool_invocation(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
            invocation_id=invocation.id, provider_result_content=str(outcome), execution_evidence=_execution_evidence(trace),
            effective_arguments=trace["effective_arguments"])
        await db.commit()
        await db.refresh(invocation)
        assert not [e for e in events if e.item_kind == "tool_permission"]
        assert invocation.permission_item_id is None and invocation.permission_state != "waiting"
        assert invocation.result_event_id == events[-1].id and invocation.effect_state == "prepared_not_started"
        assert events[-1].metadata_json["v2_payload"]["outcome"] == "approval_required"
        from app.services.content_input_receipts import _validate_input_dependencies
        from app.services.session_model_inputs import _hash

        await _validate_input_dependencies(db, tenant_id, [{"source_kind": "session_tool_invocation",
            "source_id": str(invocation.id), "source_version": _hash(str(outcome))}], user=await db.get(User, user_id))
        user = await db.get(User, user_id)
        ticket = await db.get(ApprovalRequest, approval_id)
        ticket.details = {**ticket.details, "secret_field": "synthetic-must-not-leak"}
        if action == "view_only":
            from app.models.agent import Agent, AgentPermission

            owner = User(id=uuid.uuid4(), tenant_id=tenant_id, username=f"new-{uuid.uuid4().hex}",
                email=f"{uuid.uuid4().hex}@owner.test", password_hash="synthetic", display_name="Current owner")
            db.add(owner)
            await db.flush()
            (await db.get(Agent, agent_id)).owner_user_id = owner.id
            db.add(AgentPermission(tenant_id=tenant_id, agent_id=agent_id, scope_type="user", scope_id=user_id, access_level="use"))
        other_session = ChatSession(id=uuid.uuid4(), tenant_id=tenant_id, agent_id=agent_id, user_id=user_id)
        db.add(other_session)
        await db.commit()

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            yield db

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = scoped_db
    path = f"/api/agents/{agent_id}/sessions/{session_id}/approvals/{approval_id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://enterprise-test") as client:
        current = await client.get(path)
        assert current.status_code == 200, current.text
        assert current.json()["can_resolve"] is (action != "view_only") and current.json()["status"] == "pending"
        assert "synthetic-must-not-leak" not in current.text and "execution_envelope" not in current.text
        if action == "view_only":
            assert (await client.post(path + "/resolve", json={"action": "approve"})).status_code == 403
            async with owner_sessionmaker() as db:
                assert (await db.get(ApprovalRequest, approval_id)).status == "pending"
                assert not (await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.task_type == "approval_execution"))).all()
            return
        wrong = path.replace(str(session_id), str(other_session.id))
        assert (await client.get(wrong)).status_code == 404
        assert (await client.post(wrong + "/resolve", json={"action": action})).status_code == 404
        assert (await client.post(path + "/resolve", json={"action": "allow_session"})).status_code == 422
        decision = await client.post(path + "/resolve", json={"action": action})
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == ("approved" if action == "approve" else "rejected")
        replay = await client.post(path + "/resolve", json={"action": action})
        assert replay.status_code == 200 and replay.json()["status"] == decision.json()["status"]
        opposite = await client.post(path + "/resolve", json={"action": "reject" if action == "approve" else "approve"})
        assert opposite.status_code == 409
    async with owner_sessionmaker() as db:
        ticket = await db.get(ApprovalRequest, approval_id)
        jobs = (await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.task_type == "approval_execution"))).all()
        assert len(jobs) == (1 if action == "approve" else 0)
        assert ticket.consumed_at is None
        assert not (await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id,
            ChatTranscriptEvent.item_kind == "tool_permission"))).all()
        job_id = jobs[0].id if jobs else None
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task

    if action == "approve":
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            claims = await RuntimeTaskClaimService(db=db, worker_id="inline-approval", task_types=("approval_execution",)).claim_available()
        assert [row.id for row in claims] == [job_id]
        assert await run_claimed_runtime_task(execute_claimed_approval_execution(job_id), task_id=job_id,
            claim_version=claims[0].claim_version, worker_id="inline-approval", lease_seconds=180) == "succeeded"
        assert await execute_claimed_approval_execution(job_id) == "succeeded"
    async with owner_sessionmaker() as db:
        ticket = await db.get(ApprovalRequest, approval_id)
        if action == "approve":
            assert ticket.consumed_at is not None and "Synthetic approved source." in ticket.execution_result
        else:
            assert ticket.execution_status == "rejected" and ticket.consumed_at is None
        source = await db.get(RuntimeTask, run_id)
        source.status = "completed"
        await _mark_web_terminal_boundary_delivered(db, task=source, agent_id=agent_id, session_id=session_id)
        await db.commit()
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    claimed = await service.claim_batch(worker_id="inline-result", limit=100)
    pages = await service.prepare_integration_pages(worker_id="inline-result", claimed=[item for item in claimed if item.tenant_id == tenant_id])
    assert len(pages) == 1
    receipt = await service._deliver_page(pages[0])
    assert receipt.get("ok") is True, receipt
    assert await service._mark_page_delivered(page=pages[0], worker_id="inline-result", receipt=receipt) == 1
    async with owner_sessionmaker() as db:
        successors = (await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.metadata_json["integration_page_id"].astext == str(pages[0].id)))).all()
        assert len(successors) == 1 and successors[0].parent_session_id == str(session_id)
        assert successors[0].root_user_id == user_id and "runtime-result://" in successors[0].prompt
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://enterprise-test") as client:
        current = await client.get(path)
        assert current.json()["execution_status"] == ("succeeded" if action == "approve" else "rejected")
        assert current.json()["execution_receipt"]["continuation_status"] == "delivered"
