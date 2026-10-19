"""Approved office mutations return to the original work without replaying unknown effects."""

import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.tenant_channel_config import TenantChannelConfig
from tests.integration.test_m2p_feishu_discovery_postgres import (  # noqa: F401
    ordinary_work, _exercise_ordinary_office_work,
)
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
DOCUMENT_URL = "https://company.larksuite.com/docx/company_created"


def _call(name, arguments, suffix=""):
    return {"tool_calls": [{"id": f"action-{name}{suffix}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"}


async def _successor(*, seed, client, app, app_user_sessionmaker, monkeypatch, script):
    from app.runtime import invoker
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    async with client(transport=httpx.ASGITransport(app=app), base_url="http://office-work") as api:
        response = await api.post(f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}/runs", json={
            "content": "Create the document, write the report and submit its approval in this work.", "input_id": str(uuid4())})
        assert response.status_code == 201, response.text
    run_id = UUID(response.json()["run_id"])
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        claims = await RuntimeTaskClaimService(db=db, worker_id="office-action-worker", lease_seconds=600,
            task_types=("web_chat_turn",)).claim_available()
        version = next(row.claim_version for row in claims if row.id == run_id)
        await db.commit()
    model = ScriptedLLMClient(script)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model)
    fence = set_runtime_task_fence(task_id=run_id, worker_id="office-action-worker", claim_version=version)
    try:
        await execute_web_chat_run(run_id)
    finally:
        reset_runtime_task_fence(fence)
    return run_id, model


async def test_document_and_approval_actions_deliver_in_original_work(
    ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.api import chat_sessions
    from app.models.chat_artifact import ChatArtifact
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seed = ordinary_work
    async with owner_sessionmaker() as db:
        config = await db.get(TenantChannelConfig, seed["config_id"])
        config.extra_config = {"platform_region": "lark_global"}
        await db.commit()

    async def actions(*, app, client, processor, **_kwargs):
        app.include_router(chat_sessions.router)
        mutations = []
        def provider(request):
            assert request.url.host == "open.larksuite.com", request.url
            path = request.url.path
            if "/auth/v3/" in path:
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-office-token"})
            assert request.headers["Authorization"] == "Bearer synthetic-office-token"
            if path.endswith("/spaces/get_node"):
                return httpx.Response(200, json={"code": 131004})
            if path == "/open-apis/docx/v1/documents":
                mutations.append("create")
                return httpx.Response(200, json={"code": 0, "data": {"document": {"document_id": "company_created"}}})
            if path.endswith("/metas/batch_query"):
                assert json.loads(request.content) == {"request_docs": [{"doc_token": "company_created", "doc_type": "docx"}], "with_url": True}
                return httpx.Response(200, json={"code": 0, "data": {"metas": [
                    {"doc_token": "company_created", "doc_type": "docx", "url": DOCUMENT_URL}]}})
            if path == "/open-apis/docx/v1/documents/company_created":
                return httpx.Response(200, json={"code": 0, "data": {"document": {"document_id": "company_created"}}})
            if path.endswith("/blocks/company_created/children"):
                mutations.append("append")
                assert "OFFICE_REPORT" in request.content.decode()
                return httpx.Response(200, json={"code": 0, "data": {"children": [{"block_id": "paragraph"}]}})
            if path.endswith("/approvals/company_approval"):
                return httpx.Response(200, json={"code": 0, "data": {"form": json.dumps([
                    {"id": "project_widget", "type": "input", "name": "Project"}])}})
            if path == "/open-apis/approval/v4/instances":
                mutations.append("approval")
                body = json.loads(request.content)
                assert body["user_id"] == "u_requester"
                assert json.loads(body["form"]) == [{"id": "project_widget", "type": "input", "value": "Office report"}]
                return httpx.Response(200, json={"code": 0, "data": {"instance_code": "approval_created"}})
            assert path == "/open-apis/approval/v4/instances/approval_created", request.url
            return httpx.Response(200, json={"code": 0, "data": {"instance_code": "approval_created", "status": "PENDING"}})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))
        content = f"{DOCUMENT_URL}\napproval_created\nPENDING"
        run_id, model = await _successor(seed=seed, client=client, app=app, app_user_sessionmaker=app_user_sessionmaker,
            monkeypatch=monkeypatch, script=[
                _call("feishu_doc_create", {"title": "Office report"}),
                _call("feishu_doc_append", {"document_token": "company_created", "content": "OFFICE_REPORT"}),
                _call("feishu_approval_definition", {"approval_code": "company_approval"}),
                _call("feishu_approval_create", {"approval_code": "company_approval", "user_id": "u_requester", "form": {"Project": "Office report"}}),
                _call("feishu_approval_get", {"instance_id": "approval_created"}),
                _call("write_file", {"path": "workspace/office-actions.md", "content": content}),
                {"content": "The document and approval are ready.\nDELIVERABLE: workspace/office-actions.md", "finish_reason": "stop"},
            ])
        assert DOCUMENT_URL in json.dumps(model.requests[1]["messages"])
        assert "bytedance.larkoffice.com" not in json.dumps(model.requests)
        assert mutations == ["create", "append", "approval"]
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            run = await db.get(RuntimeTask, run_id)
            assert run.status == "completed", (run.status, run.result_summary)
            from copy import deepcopy
            from uuid import uuid5
            from app.models.invocation_span import InvocationSpan
            from app.models.session_v2 import SessionToolInvocation
            from app.services.feishu_read_receipts import FEISHU_RESULT_TOOLS, FEISHU_ACTION_TOOLS, load_feishu_result
            from app.services.session_model_inputs import _hash, _origin_dependencies

            invocations = (await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == run_id))).all()
            office = [row for row in invocations if row.tool_name in FEISHU_RESULT_TOOLS]
            assert len(office) == 5
            for invocation in office:
                _, complete = await _origin_dependencies(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                    ref=f"session-tool-result:{invocation.id}")
                assert complete, invocation.tool_name
                action = invocation.tool_name in FEISHU_ACTION_TOOLS
                kind = "feishu_action" if action else "feishu_read"
                span = await db.get(InvocationSpan, uuid5(invocation.id, "feishu-action-evidence" if action else "feishu-read-evidence"))
                original = deepcopy(span.metadata_json)
                digest = original[kind]["content_hash"]
                proof = await load_feishu_result(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                    invocation=invocation, content_hash=digest, current=True)
                assert proof["reader_id"] == str(seed["user_id"])
                assert proof["connection"]["open_api_domain"] == "https://open.larksuite.com"
                if action:
                    assert proof["action"]["resource_id"] in {"company_created", "approval_created"}
                    assert "sources" not in proof  # Mutation receipts never mint a connector read grant.
                for field in ("reader_id", "model_result_id"):
                    changed = deepcopy(original)
                    changed[kind][field] = str(uuid4())
                    changed[kind]["receipt_hash"] = _hash({k: v for k, v in changed[kind].items() if k != "receipt_hash"})
                    span.metadata_json = changed
                    await db.flush()
                    assert await load_feishu_result(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                        invocation=invocation, content_hash=digest, current=True) is None
                    span.metadata_json = deepcopy(original)
                    await db.flush()
            await db.commit()
        delivery = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="office-action-boundary", canonical_validator=processor.validate,
            process_callback=processor, task_types=("web_chat_turn",))
        assert delivery["delivered"] == 1, delivery
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == run_id,
                ChatArtifact.path == "workspace/office-actions.md", ChatArtifact.session_id == seed["session_id"]))
            assert artifact is not None and artifact.snapshot_json["content_input_receipt_id"]
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://office-work") as api:
            read = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{artifact.id}/content")
            assert read.status_code == 200, read.text
            assert read.json()["content"] == content
            async with owner_sessionmaker() as db:
                (await db.get(TenantChannelConfig, seed["config_id"])).is_active = False
                await db.commit()
            revoked = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{artifact.id}/content")
            assert revoked.status_code == 403, revoked.text
            async with owner_sessionmaker() as db:
                (await db.get(TenantChannelConfig, seed["config_id"])).is_active = True
                await db.commit()
        assert mutations == ["create", "append", "approval"]

    await _exercise_ordinary_office_work(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        provider_origin="https://open.larksuite.com", follow_up=actions)


async def test_lost_mutation_response_holds_original_work_for_reconciliation_without_replay(
    ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.api import chat_sessions
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation

    async def lost_response(*, seed, app, client, report, **_kwargs):
        app.include_router(chat_sessions.router)
        writes = []
        def provider(request):
            assert request.url.host == "open.feishu.cn", request.url
            if "/auth/v3/" in request.url.path:
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-office-token"})
            assert request.url.path == "/open-apis/docx/v1/documents", request.url
            writes.append(request)
            raise httpx.ReadTimeout("Provider accepted the document but its response was lost", request=request)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))
        run_id, model = await _successor(seed=seed, client=client, app=app, app_user_sessionmaker=app_user_sessionmaker,
            monkeypatch=monkeypatch, script=[_call("feishu_doc_create", {"title": "Uncertain document"}),
                _call("feishu_doc_create", {"title": "Uncertain document"}, "-retry"),
                {"content": "The document was created.", "finish_reason": "stop"}])
        assert len(writes) == 1
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            run = await db.get(RuntimeTask, run_id)
            assert run.status == "needs_reconciliation", (run.status, run.result_summary)
            invocations = (await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == run_id))).all()
            assert len(invocations) == 1
            assert invocations[0].effect_state == "needs_reconciliation"
            assert invocations[0].result_event_id is None
            from app.services.runtime_reconciliation import get_runtime_reconciliation_task

            held = await get_runtime_reconciliation_task(db, task_id=run_id, tenant_id=seed["tenant_id"])
            assert held["retry_allowed"] is False
            assert held["supported_actions"] == ["acknowledge_tool_effect"]
        assert len(model.requests) == 1
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://office-work") as api:
            retry = await api.post(f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}/runs", json={
                "content": "Retry the original document creation.", "input_id": str(uuid4())})
            assert retry.status_code == 409, retry.text
            assert retry.json()["detail"]["code"] == "tool_effect_reconciliation_required"
            old_report = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{report.id}/content")
            assert old_report.status_code == 200, old_report.text
        assert len(writes) == 1

    await _exercise_ordinary_office_work(ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        follow_up=lost_response)
