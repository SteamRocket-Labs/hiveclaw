"""Settings revisions preserve the same authorized connection and original work."""

import hashlib
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m2p_feishu_discovery_postgres import ordinary_work, _exercise_ordinary_office_work  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_admin_credential_rotation_and_callback_settings_preserve_member_work_and_source_identity(
    ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.api import tenant_channels, chat_sessions
    from app.core.security import get_current_user
    from app.models.user import User
    from app.models.invocation_span import InvocationSpan
    from app.models.runtime_task import RuntimeTask
    from app.models.chat_artifact import ChatArtifact
    from app.runtime import invoker
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.session_model_inputs import _hash

    async def continue_work(*, seed, app, client, report, processor, provider_state, requests):
        admin = uuid4()
        async with owner_sessionmaker() as db:
            db.add(User(id=admin, tenant_id=seed["tenant_id"], username=f"admin-{admin.hex}",
                email=f"{admin.hex}@fixture.test", password_hash="synthetic", display_name="Company administrator", role="org_admin"))
            await db.commit()
        actor = {"id": seed["user_id"]}
        async def current_user():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                return await db.get(User, actor["id"])
        app.dependency_overrides[get_current_user] = current_user
        app.include_router(tenant_channels.router)
        app.include_router(chat_sessions.router)
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://reconnect-work") as api:
            refused = await api.put("/tenant-channels/feishu", json={"app_id": "cli_company_work", "app_secret": "synthetic-member-change"})
            assert refused.status_code == 403, refused.text
            async def settings(**changes):
                actor["id"] = admin
                current = await api.get("/tenant-channels")
                assert current.status_code == 200, current.text
                config = next(row for row in current.json() if row["channel_type"] == "feishu")
                saved = await api.put("/tenant-channels/feishu", json={"app_id": config["app_id"],
                    "expected_revision": config["revision"], "expected_user_id": str(admin),
                    "expected_tenant_id": str(seed["tenant_id"]), "verification_token": "synthetic-callback-token", **changes})
                assert saved.status_code == 200, saved.text
                assert saved.json()["id"] == str(seed["config_id"])
                actor["id"] = seed["user_id"]
                return saved.json()

            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                spans = (await db.scalars(select(InvocationSpan).where(InvocationSpan.tenant_id == seed["tenant_id"],
                    InvocationSpan.runtime_task_id == seed["run_id"], InvocationSpan.span_type == "feishu_read"))).all()
                # Model the already persisted v1 CN receipt shape alongside
                # current receipts, without changing any observed read fact.
                legacy = next(row for row in spans if row.name == "feishu_doc_read")
                metadata = json.loads(json.dumps(legacy.metadata_json))
                for key in ("platform_region", "open_api_domain"):
                    metadata["feishu_read"]["connection"].pop(key, None)
                metadata["feishu_read"]["receipt_hash"] = _hash({k: v for k, v in metadata["feishu_read"].items() if k != "receipt_hash"})
                legacy.metadata_json = metadata
                await db.commit()
                original = {row.id: json.dumps(row.metadata_json, sort_keys=True) for row in spans}
                assert len(original) == 2
            await settings(app_secret="synthetic-rotated-company-secret")
            provider_state["secret"] = "synthetic-rotated-company-secret"
            read = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{report.id}/content")
            assert read.status_code == 200, read.text
            assert read.json()["content"] == "COMPANY_DOCUMENT_RESULT\nCOMPANY_APPROVAL_RESULT"

            resumed = await api.post(f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}/runs",
                json={"content": "Continue this work using the reconnected company document and approval status.",
                    "input_id": str(uuid4())})
            assert resumed.status_code == 201, resumed.text
            run_id = UUID(resumed.json()["run_id"])
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                claims = await RuntimeTaskClaimService(db=db, worker_id="reconnected-member", lease_seconds=600).claim_available()
                claim_version = next(row.claim_version for row in claims if row.id == run_id)
                await db.commit()
            def call(name, arguments):
                return {"tool_calls": [{"id": f"reconnected-{name}", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"}
            model = ScriptedLLMClient([
                call("feishu_doc_read", {"document_token": "company_doc"}),
                call("feishu_approval_query", {"approval_code": "company_approval"}),
                call("write_file", {"path": "workspace/resumed-company-report.md", "content": "RESUMED_COMPANY_REPORT"}),
                {"content": "The resumed company report is ready.\nDELIVERABLE: workspace/resumed-company-report.md", "finish_reason": "stop"},
            ])
            monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model)
            before = len(requests)
            fence = set_runtime_task_fence(task_id=run_id, worker_id="reconnected-member", claim_version=claim_version)
            try:
                await execute_web_chat_run(run_id)
            finally:
                reset_runtime_task_fence(fence)
            assert len(requests) > before
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                run = await db.get(RuntimeTask, run_id)
                assert run.status == "completed", (run.status, run.result_summary)
                assert run.root_user_id == seed["user_id"] and run.root_session_id == str(seed["session_id"])
                spans = (await db.scalars(select(InvocationSpan).where(InvocationSpan.id.in_(list(original))))).all()
                assert {row.id: json.dumps(row.metadata_json, sort_keys=True) for row in spans} == original
            boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
                tenant_id=seed["tenant_id"], worker_id="resumed-office-boundary", canonical_validator=processor.validate,
                process_callback=processor, task_types=("web_chat_turn",))
            assert boundary["delivered"] == 1, boundary
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                second = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == run_id,
                    ChatArtifact.session_id == seed["session_id"], ChatArtifact.path == "workspace/resumed-company-report.md"))
                assert second is not None and second.snapshot_json["content_input_receipt_id"]
            recovered = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{second.id}/content")
            assert recovered.status_code == 200 and recovered.json()["content"] == "RESUMED_COMPANY_REPORT", recovered.text
            assert "COMPANY_DOCUMENT_RESULT" in json.dumps(model.requests[-1]["messages"])
            assert "COMPANY_APPROVAL_RESULT" in json.dumps(model.requests[-1]["messages"])

            await settings(verification_token="synthetic-new-callback-token")
            no_effects = list(requests)
            assert (await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{report.id}/content")).status_code == 200
            assert requests == no_effects
            await settings(is_active=False)
            assert (await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{report.id}/content")).status_code == 403
            await settings(is_active=True)
            assert (await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{report.id}/content")).status_code == 200
            await settings(app_id="cli_another_application", app_secret="synthetic-another-secret")
            assert (await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{report.id}/content")).status_code == 403
            await settings(app_id="cli_company_work", app_secret=provider_state["secret"])
            await settings(platform_region="lark_global", app_secret="synthetic-lark-secret")
            assert (await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{report.id}/content")).status_code == 403
            assert hashlib.sha256((seed["agent_root"] / "workspace/company-report.md").read_bytes()).hexdigest() == report.snapshot_json["content_hash"]

    await _exercise_ordinary_office_work(ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        follow_up=continue_work)
