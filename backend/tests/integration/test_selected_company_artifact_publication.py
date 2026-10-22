"""Selected-company native work preserves canonical identity and publishes its files."""

import json
import uuid

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.chat_artifact import ChatArtifact
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workspace_resource import WorkspaceResourceManifest
from app.services.chat_artifact_delivery import captured_chat_artifact_file
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run, ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
BODY = "Native result owned by the selected-company requester."
REPORT_PATH = "workspace/selected-company-report.md"


@pytest.mark.parametrize("identity", ["tenantless_admin", "foreign_home_admin", "foreign_member"])
async def test_selected_company_native_worker_publishes_without_rewriting_its_user(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, identity,
):
    import app.runtime.invoker as invoker
    from app.config import get_settings
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.llm import LLMModel
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_terminal_boundary_outbox import TerminalBoundaryCanonicalMismatch
    from app.services.secrets_provider import get_secrets_provider
    from app.services.web_chat_runtime import execute_web_chat_run

    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="Write and deliver a report from this original work.")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.workspace.async_session", app_user_sessionmaker)
    for module, name in ((agent_tools, "WORKSPACE_ROOT"), (agent_context, "PERSISTENT_DATA"),
        (agent_context, "TOOL_WORKSPACE")):
        monkeypatch.setattr(module, name, seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(agent_manager, "settings", get_settings())
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    home = None if identity == "tenantless_admin" else uuid.uuid4()
    role = "member" if identity == "foreign_member" else "platform_admin"
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        agent.default_session_permission_mode = "bypassPermissions"
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="workspace.file.write",
            allowed=True, requires_approval=False))
        await AgentManager().initialize_agent_files(db, agent, personality="Produce authorized work artifacts.",
            original_author_id=seed["user_id"])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-selected-company-provider-only")
        model.max_input_tokens = 256_000
        if home is not None:
            db.add(Tenant(id=home, name="Canonical account home", slug=f"home-{home.hex}"))
            await db.flush()
        user = await db.get(User, seed["user_id"])
        user.tenant_id, user.role = home, role
        await db.commit()
    model_client = ScriptedLLMClient([
        {"tool_calls": [{"id": "selected-company-write", "type": "function", "function": {
            "name": "fs_write", "arguments": json.dumps({"path": REPORT_PATH, "content": BODY})}}], "finish_reason": "tool_calls"},
        {"content": f"The report is ready: [Report]({REPORT_PATH})", "finish_reason": "stop"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model_client)
    fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        if identity == "foreign_member":
            with pytest.raises((RuntimeError, TerminalBoundaryCanonicalMismatch)) as refusal:
                await execute_web_chat_run(seed["run_id"])
            causes, seen, error = [], set(), refusal.value
            while error is not None and id(error) not in seen:
                seen.add(id(error))
                causes.append(error)
                error = error.__cause__ or error.__context__
            assert any(isinstance(error, RuntimeError) and str(error) in {
                f"User {seed['user_id']} not found",
                f"tenant boundary mismatch for run {seed['run_id']}: user tenant does not match agent tenant",
            } for error in causes), causes
        else:
            await execute_web_chat_run(seed["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    async with owner_sessionmaker() as db:
        canonical = await db.get(User, seed["user_id"])
        assert (canonical.tenant_id, canonical.role) == (home, role)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        task = await db.get(RuntimeTask, seed["run_id"])
        artifacts = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == task.id)))
        observed = {"identity": identity, "status": task.status, "summary": task.result_summary,
            "metadata": task.metadata_json, "model_requests": len(model_client.requests),
            "artifact_ids": [str(a.id) for a in artifacts], "file_exists": (seed["agent_root"] / REPORT_PATH).exists()}
        (tmp_path / "selected-company-artifact-publication.json").write_text(json.dumps(observed, default=str, indent=2))
        if identity == "foreign_member":
            assert task.status != "completed" and model_client.requests == [] and artifacts == [], observed
            assert not observed["file_exists"], observed
            return
        assert task.status == "completed" and len(model_client.requests) == 2, observed
        assert (seed["agent_root"] / REPORT_PATH).read_text() == BODY
        assert len(artifacts) == 1, observed
        artifact = artifacts[0]
        assert (artifact.tenant_id, artifact.agent_id, artifact.session_id, artifact.owner_user_id) == (
            seed["tenant_id"], seed["agent_id"], seed["session_id"], seed["user_id"])
        assert artifact.path == REPORT_PATH and artifact.snapshot_json["content_input_receipt_id"]
        assert artifact.snapshot_json["tool_invocation_id"] and artifact.snapshot_json["execution_evidence_ref"]
        with captured_chat_artifact_file(artifact, seed["agent_root"]) as (captured, _source):
            assert captured is not None and captured.read_text() == BODY
        assert not captured.exists()
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.tenant_id == seed["tenant_id"], WorkspaceResourceManifest.agent_id == seed["agent_id"],
            WorkspaceResourceManifest.path == REPORT_PATH))
        assert manifest.owner_user_id == seed["user_id"]

    from app.api import files
    from app.core.security import create_access_token, decode_access_token
    from app.database import get_db

    monkeypatch.setattr("app.services.audit_logger.async_session", app_user_sessionmaker)

    async def selected_db(request: Request):
        claims = decode_access_token(request.headers["authorization"].removeprefix("Bearer "))
        selected = request.headers.get("x-tenant-id") if claims["role"] == "platform_admin" else None
        async with app_user_sessionmaker() as db:
            # Match middleware's optimistic token scope; real authentication
            # still checks the live canonical role and audits company selection.
            await pin_rls_tenant_context(db, uuid.UUID(selected or claims["tid"]))
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[get_db] = selected_db
    headers = {"Authorization": "Bearer " + create_access_token(str(seed["user_id"]), role, str(home) if home else None),
        "X-Tenant-Id": str(seed["tenant_id"])}
    url = f"/agents/{seed['agent_id']}/files/artifacts/{artifact.id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://selected-company.test", headers=headers) as client:
        content = await client.get(url + "/content")
        assert content.status_code == 200 and content.json()["content"] == BODY, content.text
        assert content.json()["operator_view"] is False
        download = await client.get(url + "/download")
        assert download.status_code == 200 and download.content == BODY.encode(), download.text
        async with owner_sessionmaker() as db:
            canonical = await db.get(User, seed["user_id"])
            assert (canonical.tenant_id, canonical.role) == (home, role)
            canonical.role = "member"
            await db.commit()
        for suffix in ("/content", "/download"):
            denied = await client.get(url + suffix)
            assert denied.status_code in {403, 404} and BODY.encode() not in denied.content, denied.text
    async with owner_sessionmaker() as db:
        canonical = await db.get(User, seed["user_id"])
        assert (canonical.tenant_id, canonical.role) == (home, "member")
