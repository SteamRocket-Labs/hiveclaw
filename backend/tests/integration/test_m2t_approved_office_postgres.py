"""A real approved office job returns evidence to the original requester work."""

import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m2p_feishu_discovery_postgres import ordinary_work, _exercise_ordinary_office_work  # noqa: F401
from tests.integration.test_m2s_office_actions_postgres import _call, _successor
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
DOCUMENT_URL = "https://company.feishu.cn/docx/approved_document"


@pytest.mark.parametrize(("tool_name", "outcome"), [
    ("feishu_doc_create", "confirmed"), ("feishu_doc_create", "unknown"),
    ("feishu_approval_definition", "confirmed"),
])
async def test_approved_office_job_is_consumed_in_original_private_work(
    ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tool_name, outcome,  # noqa: F811
):
    from app.api import chat_sessions, enterprise
    from app.core.security import get_current_user
    from app.models.audit import ApprovalRequest
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.guard_policy import GuardPolicy
    from app.models.chat_session import ChatSession
    from app.models.runtime_result import RuntimeResultIntegrationPage, RuntimeResultObject
    from app.models.tenant_channel_config import TenantChannelConfig
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.runtime import invoker
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.runtime_result_sources import validate_runtime_return_dependency
    from app.services.runtime_result_sources import validate_runtime_result_sources
    from app.services.feishu_read_receipts import load_approved_feishu_result
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_chat_runtime import execute_web_chat_run

    seed = ordinary_work
    arguments = {"title": "Approved report"} if tool_name == "feishu_doc_create" else {"approval_code": "approved_definition"}
    expected_content = DOCUMENT_URL if tool_name == "feishu_doc_create" else "APPROVED_DEFINITION_RESULT"
    approver = uuid4()
    async with owner_sessionmaker() as db:
        db.add(GuardPolicy(tenant_id=seed["tenant_id"], zone_guard={"tool_rules": [
            {"tools": [tool_name], "decision": "require_approval"}]}))
        db.add(User(id=approver, tenant_id=seed["tenant_id"], username=f"approver-{approver.hex}",
            email=f"{approver.hex}@fixture.test", password_hash="synthetic", display_name="Company approver", role="org_admin"))
        await db.commit()

    async def approved_work(*, app, client, processor, **_kwargs):
        app.include_router(chat_sessions.router)
        app.include_router(enterprise.router)
        actor = {"id": seed["user_id"]}
        async def current_user():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                return await db.get(User, actor["id"])
        app.dependency_overrides[get_current_user] = current_user
        mutations = []
        def provider(request):
            assert request.url.host == "open.feishu.cn", request.url
            if "/auth/v3/" in request.url.path:
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-approved-token"})
            assert request.headers["Authorization"] == "Bearer synthetic-approved-token"
            if request.url.path.endswith("/approvals/approved_definition"):
                mutations.append(request)
                return httpx.Response(200, json={"code": 0, "data": {"form": json.dumps([
                    {"id": "widget", "type": "input", "name": expected_content}])}})
            if request.url.path == "/open-apis/docx/v1/documents":
                mutations.append(request)
                assert json.loads(request.content) == {"title": "Approved report"}
                if outcome == "unknown":
                    raise httpx.ReadTimeout("Response lost after dispatch", request=request)
                return httpx.Response(200, json={"code": 0, "data": {"document": {"document_id": "approved_document"}}})
            assert request.url.path.endswith("/metas/batch_query"), request.url
            return httpx.Response(200, json={"code": 0, "data": {"metas": [{
                "doc_token": "approved_document", "doc_type": "docx", "url": DOCUMENT_URL}]}})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))
        source_run, _ = await _successor(seed=seed, client=client, app=app, app_user_sessionmaker=app_user_sessionmaker,
            monkeypatch=monkeypatch, script=[_call(tool_name, arguments),
                {"content": "The document request is waiting for company approval.", "finish_reason": "stop"}])
        assert mutations == []
        async with owner_sessionmaker() as db:
            ticket = await db.scalar(select(ApprovalRequest).where(ApprovalRequest.tenant_id == seed["tenant_id"],
                ApprovalRequest.tool_name == tool_name))
            assert ticket is not None and ticket.status == "pending"
            approval_id = ticket.id
        await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="approval-origin-boundary", canonical_validator=processor.validate,
            process_callback=processor, task_types=("web_chat_turn",))
        actor["id"] = approver
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://approved-work") as api:
            response = await api.post(f"/enterprise/approvals/{approval_id}/resolve", json={"action": "approve"})
            assert response.status_code == 200, response.text
            again = await api.post(f"/enterprise/approvals/{approval_id}/resolve", json={"action": "approve"})
            assert again.status_code == 400 and again.json()["detail"] == "Approval already resolved", again.text
        actor["id"] = seed["user_id"]
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            ticket = await db.get(ApprovalRequest, approval_id)
            job = ticket.execution_task_id
            claims = await RuntimeTaskClaimService(db=db, worker_id="approved-office", task_types=("approval_execution",),
                lease_seconds=600).claim_available()
            assert [row.id for row in claims] == [job]
            version = claims[0].claim_version
            await db.commit()
        expected = "succeeded" if outcome == "confirmed" else "needs_reconciliation"
        assert await run_claimed_runtime_task(execute_claimed_approval_execution(job), task_id=job,
            claim_version=version, worker_id="approved-office", lease_seconds=600) == expected
        assert await execute_claimed_approval_execution(job) == expected
        assert len(mutations) == 1
        async with owner_sessionmaker() as db:
            ticket = await db.get(ApprovalRequest, approval_id)
            task = await db.get(RuntimeTask, job)
            assert ticket.requested_by == seed["user_id"] and ticket.resolved_by == approver
            assert task.root_user_id == seed["user_id"] and task.parent_session_id == str(seed["session_id"])
            assert ticket.execution_status == expected
            if outcome == "confirmed":
                assert expected_content in ticket.execution_result
            else:
                assert task.status == "needs_reconciliation" and ticket.consumed_at is not None
                assert ticket.execution_receipt["side_effect_state"] == "unknown"
                assert ticket.execution_receipt["automatic_replay"] is False
        service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
        claimed = await service.claim_batch(worker_id="approved-return", limit=100)
        pages = await service.prepare_integration_pages(worker_id="approved-return",
            claimed=[row for row in claimed if row.tenant_id == seed["tenant_id"]])
        assert len(pages) == 1
        receipt = await service._deliver_page(pages[0])
        assert receipt.get("ok") is True, receipt
        assert await service._mark_page_delivered(page=pages[0], worker_id="approved-return", receipt=receipt) == 1
        if outcome == "unknown":
            assert receipt["status"] == "projected" and receipt["consumer"] == "session_projection"
            assert pages[0].delivery_mode == "session_projection"
            async with owner_sessionmaker() as db:
                continued = await db.scalar(select(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant_id"],
                    RuntimeTask.metadata_json["integration_page_id"].astext == str(pages[0].id)))
                assert continued is None
                event = await db.scalar(select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.tenant_id == seed["tenant_id"], ChatTranscriptEvent.session_id == seed["session_id"],
                    ChatTranscriptEvent.metadata_json["integration_page_id"].astext == str(pages[0].id),
                    ChatTranscriptEvent.event_type == "agent_task_notification"))
                assert event is not None
                assert DOCUMENT_URL not in event.content
                blocked = await db.scalar(select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.tenant_id == seed["tenant_id"], ChatTranscriptEvent.session_id == seed["session_id"],
                    ChatTranscriptEvent.metadata_json["integration_page_id"].astext == str(pages[0].id),
                    ChatTranscriptEvent.event_type == "runtime_action_blocked"))
                assert blocked is not None and blocked.metadata_json["status"] == "needs_reconciliation"
                assert blocked.metadata_json["result_ref"] == pages[0].manifest["items"][0]["result_ref"]
            assert len(mutations) == 1
            return
        result_ref = pages[0].manifest["items"][0]["result_ref"]
        # Pages expose references; only the governed reader may disclose result bytes.
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            page = await db.get(RuntimeResultIntegrationPage, pages[0].id)
            user = await db.get(User, seed["user_id"])
            dependency = {"source_kind": "runtime_result_page", "source_id": str(page.id),
                "source_version": page.manifest_sha256}
            await validate_runtime_return_dependency(db, seed["tenant_id"], dependency, user=user, runtime_request=None)
            result = await db.get(RuntimeResultObject, UUID(result_ref.split("/")[2]))
            result_dependency = {"source_kind": "runtime_result",
                "source_id": f"{seed['agent_id']}\n{seed['session_id']}\n{result.id}", "source_version": result.sha256}
            await validate_runtime_return_dependency(db, seed["tenant_id"], result_dependency, user=user, runtime_request=None)
            session = await db.get(ChatSession, seed["session_id"])
            with pytest.raises(ContentInputReceiptError):
                await validate_runtime_result_sources(db, None, result, user=await db.get(User, approver), session=session)
            ticket = await db.get(ApprovalRequest, approval_id)
            proof = await load_approved_feishu_result(db, approval=ticket)
            assert proof is not None and proof["requester_id"] == str(seed["user_id"])
            assert proof["connection"]["id"] == str(seed["config_id"])
            receipt = ticket.execution_receipt
            ticket.execution_receipt = {**receipt, "runtime_evidence": {}}
            assert await load_approved_feishu_result(db, approval=ticket) is None
            ticket.execution_receipt = receipt
            config = await db.get(TenantChannelConfig, seed["config_id"])
            config.is_active = False
            await db.flush()
            await validate_runtime_return_dependency(db, seed["tenant_id"], dependency, user=user, runtime_request=None)
            with pytest.raises(ContentInputReceiptError, match="original company approval result"):
                await validate_runtime_return_dependency(db, seed["tenant_id"], result_dependency, user=user, runtime_request=None)
            await db.rollback()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            continued = await db.scalar(select(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant_id"],
                RuntimeTask.metadata_json["integration_page_id"].astext == str(pages[0].id)))
            assert continued and continued.root_user_id == seed["user_id"] and continued.parent_session_id == str(seed["session_id"])
            continuation_id = continued.id
            claims = await RuntimeTaskClaimService(db=db, worker_id="approved-consumer", task_types=("web_chat_turn",),
                lease_seconds=600).claim_available()
            version = next(row.claim_version for row in claims if row.id == continuation_id)
            await db.commit()
        model = ScriptedLLMClient([_call("read_runtime_result", {"result_ref": result_ref}),
            _call("write_file", {"path": "workspace/approved-office.md", "content": expected_content}),
            {"content": "The approved document is ready.\nDELIVERABLE: workspace/approved-office.md", "finish_reason": "stop"}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kw: model)
        assert await run_claimed_runtime_task(execute_web_chat_run(continuation_id), task_id=continuation_id,
            claim_version=version, worker_id="approved-consumer", lease_seconds=600) is None
        assert expected_content in json.dumps(model.requests[1]["messages"])
        assert expected_content not in json.dumps(model.requests[0]["messages"])
        await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="approved-consumer-boundary", canonical_validator=processor.validate,
            process_callback=processor, task_types=("web_chat_turn",))
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == continuation_id,
                ChatArtifact.path == "workspace/approved-office.md"))
            assert artifact is not None and artifact.snapshot_json["content_input_receipt_id"]
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://approved-work") as api:
            read = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{artifact.id}/content")
            assert read.status_code == 200 and read.json()["content"] == expected_content, read.text
        assert len(mutations) == 1

    await _exercise_ordinary_office_work(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        follow_up=approved_work)
