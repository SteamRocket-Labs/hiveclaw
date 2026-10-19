"""A fresh approved action recovers in original work after an admin connection update."""

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
DIAGNOSTIC = "RECOVERY_REQUIRED_UPDATE_CONNECTION"
DOCUMENT_URL = "https://company.feishu.cn/docx/recovered_document"


async def test_company_update_requires_new_approval_and_returns_result_to_same_work(
    ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.api import chat_sessions, enterprise, tenant_channels
    from app.core.security import get_current_user
    from app.models.audit import ApprovalRequest
    from app.models.chat_artifact import ChatArtifact
    from app.models.guard_policy import GuardPolicy
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.runtime import invoker
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_chat_runtime import execute_web_chat_run

    seed, approver = ordinary_work, uuid4()
    async with owner_sessionmaker() as db:
        db.add(GuardPolicy(tenant_id=seed["tenant_id"], zone_guard={"tool_rules": [
            {"tools": ["feishu_doc_create"], "decision": "require_approval"}]}))
        db.add(User(id=approver, tenant_id=seed["tenant_id"], username=f"recover-{approver.hex}",
            email=f"{approver.hex}@fixture.test", password_hash="synthetic", display_name="Connection admin", role="org_admin"))
        await db.commit()

    async def recovery(*, app, client, processor, **_kwargs):
        app.include_router(chat_sessions.router)
        app.include_router(enterprise.router)
        app.include_router(tenant_channels.router)
        actor = {"id": seed["user_id"]}
        async def current_user():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                return await db.get(User, actor["id"])
        app.dependency_overrides[get_current_user] = current_user
        old_secret, new_secret = "synthetic-company-work-secret", "synthetic-renewed-company-secret"
        state, requests, mutations, models = {"reconnected": False}, [], [], []
        def provider(request):
            assert request.url.host == "open.feishu.cn"
            requests.append(request)
            if "/auth/v3/" in request.url.path:
                assert json.loads(request.content)["app_secret"] == (new_secret if state["reconnected"] else old_secret)
                if not state["reconnected"]:
                    return httpx.Response(401, json={"code": 90041, "msg": f"{DIAGNOSTIC}: {old_secret}"})
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-renewed-office-token"})
            assert request.headers["Authorization"] == "Bearer synthetic-renewed-office-token"
            if request.url.path == "/open-apis/docx/v1/documents":
                mutations.append(request)
                return httpx.Response(200, json={"code": 0, "data": {"document": {"document_id": "recovered_document"}}})
            assert request.url.path.endswith("/metas/batch_query")
            return httpx.Response(200, json={"code": 0, "data": {"metas": [{
                "doc_token": "recovered_document", "doc_type": "docx", "url": DOCUMENT_URL}]}})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))

        async def drain_boundary():
            await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
                tenant_id=seed["tenant_id"], worker_id="office-recovery-boundary", canonical_validator=processor.validate,
                process_callback=processor, task_types=("web_chat_turn",))

        async def request_action():
            await _successor(seed=seed, client=client, app=app, app_user_sessionmaker=app_user_sessionmaker,
                monkeypatch=monkeypatch, script=[_call("feishu_doc_create", {"title": "Recovery report"}),
                    {"content": "Waiting for company approval.", "finish_reason": "stop"}])
            await drain_boundary()
            async with owner_sessionmaker() as db:
                row = await db.scalar(select(ApprovalRequest).where(ApprovalRequest.tenant_id == seed["tenant_id"],
                    ApprovalRequest.tool_name == "feishu_doc_create", ApprovalRequest.status == "pending"))
                assert row is not None
                return row.id

        async def approve_and_execute(approval_id, expected):
            actor["id"] = approver
            async with client(transport=httpx.ASGITransport(app=app), base_url="http://recovery") as api:
                approved = await api.post(f"/enterprise/approvals/{approval_id}/resolve", json={"action": "approve"})
                assert approved.status_code == 200, approved.text
            actor["id"] = seed["user_id"]
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                row = await db.get(ApprovalRequest, approval_id)
                job = row.execution_task_id
                claims = await RuntimeTaskClaimService(db=db, worker_id="office-recovery", task_types=("approval_execution",),
                    lease_seconds=600).claim_available()
                assert [c.id for c in claims] == [job]
                version = claims[0].claim_version
                await db.commit()
            assert await run_claimed_runtime_task(execute_claimed_approval_execution(job), task_id=job,
                claim_version=version, worker_id="office-recovery", lease_seconds=600) == expected
            assert await execute_claimed_approval_execution(job) == expected
            return job

        async def consume_result(content, path):
            service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
            rows = await service.claim_batch(worker_id="office-recovery-return", limit=100)
            pages = await service.prepare_integration_pages(worker_id="office-recovery-return", claimed=rows)
            assert len(pages) == 1
            page = pages[0]
            receipt = await service._deliver_page(page)
            assert receipt.get("ok") is True, receipt
            assert await service._mark_page_delivered(page=page, worker_id="office-recovery-return", receipt=receipt) == 1
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                task = await db.scalar(select(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant_id"],
                    RuntimeTask.metadata_json["integration_page_id"].astext == str(page.id)))
                assert task and task.root_user_id == seed["user_id"] and task.parent_session_id == str(seed["session_id"])
                task_id = task.id
                claims = await RuntimeTaskClaimService(db=db, worker_id="office-recovery-reader", task_types=("web_chat_turn",),
                    lease_seconds=600).claim_available()
                version = next(c.claim_version for c in claims if c.id == task_id)
                await db.commit()
            model = ScriptedLLMClient([_call("read_runtime_result", {"result_ref": page.manifest["items"][0]["result_ref"]}),
                _call("write_file", {"path": path, "content": content}),
                {"content": f"The current outcome is recorded.\nDELIVERABLE: {path}", "finish_reason": "stop"}])
            models.append(model)
            monkeypatch.setattr(invoker, "create_llm_client", lambda **_kw: model)
            await run_claimed_runtime_task(execute_web_chat_run(task_id), task_id=task_id,
                claim_version=version, worker_id="office-recovery-reader", lease_seconds=600)
            assert content in json.dumps(model.requests[1]["messages"])
            await drain_boundary()
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == task_id, ChatArtifact.path == path))
                assert artifact and artifact.snapshot_json["content_input_receipt_id"]
                return artifact.id

        first = await request_action()
        old_job = await approve_and_execute(first, "failed")
        assert mutations == []
        failed_artifact = await consume_result(DIAGNOSTIC, "workspace/connection-feedback.md")
        body = {"app_id": "cli_company_work", "app_secret": new_secret, "verification_token": "synthetic-callback-token",
            "expected_tenant_id": str(seed["tenant_id"]), "expected_user_id": str(approver)}
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://recovery") as api:
            refused = await api.put("/tenant-channels/feishu", json=body)
            assert refused.status_code == 403, refused.text
            actor["id"] = approver
            listed = await api.get("/tenant-channels")
            assert listed.status_code == 200, listed.text
            existing = next(c for c in listed.json() if c["id"] == str(seed["config_id"]))
            body["expected_revision"] = existing["revision"]
            changed = await api.put("/tenant-channels/feishu", json=body)
            assert changed.status_code == 200 and changed.json()["revision"] != existing["revision"], changed.text
            assert new_secret not in changed.text
            stale = await api.put("/tenant-channels/feishu", json=body)
            assert stale.status_code == 409, stale.text
            actor["id"] = seed["user_id"]
            retained = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{failed_artifact}/content")
            assert retained.status_code == 200 and retained.json()["content"] == DIAGNOSTIC, retained.text
        state["reconnected"] = True
        before = len(requests)
        assert await execute_claimed_approval_execution(old_job) == "failed" and len(requests) == before
        second = await request_action()
        assert second != first and mutations == []
        await approve_and_execute(second, "succeeded")
        assert len(mutations) == 1
        ready = await consume_result(DOCUMENT_URL, "workspace/recovered-office.md")
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://recovery") as api:
            delivered = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{ready}/content")
            assert delivered.status_code == 200 and delivered.json()["content"] == DOCUMENT_URL, delivered.text
        async with owner_sessionmaker() as db:
            original = await db.get(ApprovalRequest, first)
            assert DIAGNOSTIC in original.execution_result and old_secret not in original.execution_result
        assert all(secret not in json.dumps([m.requests for m in models]) for secret in (old_secret, new_secret, "synthetic-renewed-office-token"))
        assert len(mutations) == 1

    await _exercise_ordinary_office_work(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, follow_up=recovery)


async def test_direct_office_failure_is_a_private_source_checked_diagnostic(
    ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.api import chat_sessions
    from app.models.chat_artifact import ChatArtifact
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seed = ordinary_work
    async def failure(*, app, client, processor, **_kwargs):
        app.include_router(chat_sessions.router)
        def provider(request):
            assert request.url.host == "open.feishu.cn"
            if "/auth/v3/" in request.url.path:
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-private-diagnostic-token"})
            assert request.headers["Authorization"] == "Bearer synthetic-private-diagnostic-token"
            if request.url.path.endswith("/spaces/get_node"):
                return httpx.Response(200, json={"code": 131004})
            assert request.url.path.endswith("/documents/protected_doc/raw_content")
            return httpx.Response(403, json={"code": 90042,
                "msg": f"{DIAGNOSTIC}: synthetic-private-diagnostic-token"})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))
        run, model = await _successor(seed=seed, client=client, app=app, app_user_sessionmaker=app_user_sessionmaker,
            monkeypatch=monkeypatch, script=[_call("feishu_doc_read", {"document_token": "protected_doc"}),
                _call("write_file", {"path": "workspace/private-diagnostic.md", "content": DIAGNOSTIC}),
                {"content": "The access failure needs review.\nDELIVERABLE: workspace/private-diagnostic.md", "finish_reason": "stop"}])
        assert DIAGNOSTIC in json.dumps(model.requests[1]["messages"])
        assert "synthetic-private-diagnostic-token" not in json.dumps(model.requests)
        await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="direct-diagnostic-boundary", canonical_validator=processor.validate,
            process_callback=processor, task_types=("web_chat_turn",))
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == run,
                ChatArtifact.path == "workspace/private-diagnostic.md"))
            assert artifact and artifact.snapshot_json["content_input_receipt_id"]
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://diagnostic") as api:
            read = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{artifact.id}/content")
            assert read.status_code == 200 and read.json()["content"] == DIAGNOSTIC, read.text

    await _exercise_ordinary_office_work(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, follow_up=failure)
