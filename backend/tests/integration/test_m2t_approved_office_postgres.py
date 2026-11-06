"""A real approved office job returns evidence to the original requester work."""

import json
from uuid import uuid4

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
    ("feishu_doc_create", "denied"), ("feishu_doc_create", "auth_failed"),
    ("feishu_approval_definition", "denied"),
])
async def test_approved_office_job_is_consumed_in_original_private_work(
    ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tool_name, outcome,  # noqa: F811
):
    from app.api import chat_sessions, enterprise
    from app.core.security import get_current_user
    from app.models.audit import ApprovalRequest
    from app.models.chat_artifact import ChatArtifact
    from app.models.guard_policy import GuardPolicy
    from app.models.tenant_channel_config import TenantChannelConfig
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.runtime import invoker
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task
    from app.services.feishu_read_receipts import load_approved_feishu_result
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_chat_runtime import execute_web_chat_run

    seed = ordinary_work
    arguments = {"title": "Approved report"} if tool_name == "feishu_doc_create" else {"approval_code": "approved_definition"}
    expected_content = DOCUMENT_URL if tool_name == "feishu_doc_create" else "APPROVED_DEFINITION_RESULT"
    failure = outcome in {"denied", "auth_failed"}
    provider_code = 90032 if outcome == "auth_failed" else 90031 if tool_name == "feishu_doc_create" else 90033
    if failure:
        expected_content = str(provider_code)
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
                if outcome == "auth_failed":
                    return httpx.Response(401, json={"code": provider_code, "msg": "Fixture credential rejected"})
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-approved-token"})
            assert request.headers["Authorization"] == "Bearer synthetic-approved-token"
            if request.url.path.endswith("/approvals/approved_definition"):
                mutations.append(request)
                if failure:
                    return httpx.Response(403, json={"code": provider_code, "msg": "Fixture permission denied"})
                return httpx.Response(200, json={"code": 0, "data": {"form": json.dumps([
                    {"id": "widget", "type": "input", "name": expected_content}])}})
            if request.url.path == "/open-apis/docx/v1/documents":
                mutations.append(request)
                assert json.loads(request.content) == {"title": "Approved report"}
                if failure:
                    return httpx.Response(403, json={"code": provider_code, "msg": "Fixture permission denied"})
                if outcome == "unknown":
                    raise httpx.ReadTimeout("Response lost after dispatch", request=request)
                return httpx.Response(200, json={"code": 0, "data": {"document": {"document_id": "approved_document"}}})
            assert request.url.path.endswith("/metas/batch_query"), request.url
            return httpx.Response(200, json={"code": 0, "data": {"metas": [{
                "doc_token": "approved_document", "doc_type": "docx", "url": DOCUMENT_URL}]}})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))
        source_run, _ = await _successor(seed=seed, client=client, app=app, app_user_sessionmaker=app_user_sessionmaker,
            monkeypatch=monkeypatch, script=[_call(tool_name, arguments),
                {"content": "The office request is waiting for company approval.", "finish_reason": "stop"}])
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
        expected = "failed" if failure else "succeeded" if outcome == "confirmed" else "needs_reconciliation"
        assert await run_claimed_runtime_task(execute_claimed_approval_execution(job), task_id=job,
            claim_version=version, worker_id="approved-office", lease_seconds=600) == expected
        assert await execute_claimed_approval_execution(job) == expected
        expected_calls = 0 if outcome == "auth_failed" else 1
        assert len(mutations) == expected_calls
        async with owner_sessionmaker() as db:
            ticket = await db.get(ApprovalRequest, approval_id)
            task = await db.get(RuntimeTask, job)
            assert ticket.requested_by == seed["user_id"] and ticket.resolved_by == approver
            assert task.root_user_id == seed["user_id"] and task.parent_session_id == str(seed["session_id"])
            assert ticket.execution_status == expected
            if outcome == "confirmed":
                assert expected_content in ticket.execution_result
            elif failure:
                assert task.status == "failed" and ticket.execution_receipt["error"]["provider_code"] == provider_code
                assert ticket.execution_receipt["error"]["error_class"] == "auth_or_permission"
            else:
                assert task.status == "needs_reconciliation" and ticket.consumed_at is not None
                assert ticket.execution_receipt["side_effect_state"] == "unknown"
                assert ticket.execution_receipt["automatic_replay"] is False
        service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
        claimed = await service.claim_batch(worker_id="approved-return", limit=100)
        pages = await service.prepare_integration_pages(worker_id="approved-return",
            claimed=[row for row in claimed if row.tenant_id == seed["tenant_id"]])
        # A4b: a member's own web run suspends for the company decision and
        # resumes IN PLACE with the outcome as its continuation input, so this
        # lane no longer produces a successor-turn result page.  The
        # result-page governed reader remains the delivery path for channel and
        # machine origins (covered by the m2ac company-approval flows).
        assert pages == []
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            ticket = await db.get(ApprovalRequest, approval_id)
            proof = await load_approved_feishu_result(db, approval=ticket)
            if outcome == "unknown":
                assert proof is None, "an uncertain effect has no confirmed result proof"
            else:
                assert proof is not None and proof["requester_id"] == str(seed["user_id"])
                assert proof["connection"]["id"] == str(seed["config_id"])
            if failure:
                assert proof["failure"]["provider_code"] == provider_code and proof["outcome"] == "failed"
            receipt = ticket.execution_receipt
            ticket.execution_receipt = {**receipt, "runtime_evidence": {}}
            assert await load_approved_feishu_result(db, approval=ticket) is None
            ticket.execution_receipt = receipt
            if failure:
                from copy import deepcopy
                from app.services.session_model_inputs import _hash

                for key, value in (("open_api_domain", "https://open.larksuite.com"), ("provider_code", provider_code + 1)):
                    altered = deepcopy(receipt)
                    sealed = altered["runtime_evidence"]["feishu_approved_result"]
                    sealed["failure"][key] = value
                    sealed["receipt_hash"] = _hash({k: v for k, v in sealed.items() if k != "receipt_hash"})
                    ticket.execution_receipt = altered
                    assert await load_approved_feishu_result(db, approval=ticket) is None
                ticket.execution_receipt = receipt
            config = await db.get(TenantChannelConfig, seed["config_id"])
            config.is_active = False
            await db.flush()
            assert await load_approved_feishu_result(db, approval=ticket) is None
            await db.rollback()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            continuation_id = source_run
            resumed = await db.get(RuntimeTask, continuation_id)
            assert resumed.root_user_id == seed["user_id"] and resumed.parent_session_id == str(seed["session_id"])
            claims = await RuntimeTaskClaimService(db=db, worker_id="approved-consumer", task_types=("web_chat_turn",),
                lease_seconds=600).claim_available()
            version = next(row.claim_version for row in claims if row.id == continuation_id)
            await db.commit()
        model = ScriptedLLMClient([
            _call("write_file", {"path": "workspace/approved-office.md", "content": expected_content}),
            {"content": ("The provider response needs review." if failure else "The approved document is ready.")
                + "\nDELIVERABLE: workspace/approved-office.md", "finish_reason": "stop"}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kw: model)
        assert await run_claimed_runtime_task(execute_web_chat_run(continuation_id), task_id=continuation_id,
            claim_version=version, worker_id="approved-consumer", lease_seconds=600) is None
        # The SAME Run continues in place: its resumed round carries the honest
        # approval outcome to the member's model, exactly once.
        assert len(model.requests) == 2
        resumed_messages = json.dumps(model.requests[0]["messages"])
        assert "[Approval tool result]" in resumed_messages
        if outcome == "confirmed":
            assert expected_content in resumed_messages
        elif outcome == "unknown":
            assert "Status: needs_reconciliation" in resumed_messages
            assert DOCUMENT_URL not in resumed_messages
            assert "Verify its outcome before retrying" in resumed_messages
        if failure:
            assert ("reconnect" if outcome == "auth_failed" else "review_permissions") in resumed_messages
            assert ("Fixture credential rejected" if outcome == "auth_failed" else "Fixture permission denied") in resumed_messages
        assert expected_content in json.dumps(model.requests[1]["messages"])
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
        assert len(mutations) == expected_calls

    await _exercise_ordinary_office_work(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        follow_up=approved_work)
