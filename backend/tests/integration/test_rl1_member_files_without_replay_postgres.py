"""Member files use object authority; publication and outward effects keep source checks."""

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import tenant_scoped_session
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call
from tests.integration.test_r15e_artifact_graph_reads_postgres import _profile
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _call(name, identity, **arguments):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


@pytest.fixture
async def member_web(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    from app.api import chat_sessions, files, upload
    from app.config import get_settings
    from app.core.security import get_current_user
    from app.database import get_db
    from app.models.agent import Agent, AgentPermission
    from app.models.capability_policy import CapabilityPolicy
    from app.models.llm import LLMModel
    from app.models.user import User
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.secrets_provider import get_secrets_provider

    class SummaryClient:
        async def stream(self, **_kwargs):
            return SimpleNamespace(content=json.dumps({"summary": "Local member-file continuation."}))

        async def close(self):
            pass

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args, **_kwargs: SummaryClient())

    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="Read my CSV and write my report.")
    n = SimpleNamespace(**seed, reader=seed["user_id"])
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(n.data_root))
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", n.data_root)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", n.data_root)
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", n.data_root)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", n.data_root)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", n.data_root)
    monkeypatch.setattr(agent_manager, "settings", settings)
    (n.agent_root / "workspace").rmdir()
    n.agent_root.rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, n.agent_id)
        await AgentManager().initialize_agent_files(db, agent, original_author_id=n.user_id)
        (await db.get(User, n.user_id)).role = "member"
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-rl1-model")
        model.max_input_tokens = 256_000
        db.add(AgentPermission(tenant_id=n.tenant_id, agent_id=n.agent_id, scope_type="company", access_level="use"))
        for capability in ("workspace.file.read", "workspace.file.write"):
            db.add(CapabilityPolicy(tenant_id=n.tenant_id, agent_id=n.agent_id, capability=capability,
                allowed=True, requires_approval=False))
        await db.commit()

    async def current_user():
        async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
            return await db.get(User, n.reader)

    async def scoped_db():
        async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
            yield db
            await db.commit()

    app = FastAPI()
    for router in (files.router, upload.router, chat_sessions.router):
        app.include_router(router)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = scoped_db
    monkeypatch.setattr(files, "_load_download_user_from_jwt", lambda **_kwargs: current_user())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local",
        headers={"Authorization": "Bearer fixture-member"}) as client:
        n.client = client
        yield n


async def _run(n, script, monkeypatch, run_id=None, worker="kernel-worker-1", version=1):
    from app.runtime import invoker
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    model = ScriptedLLMClient(script)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model)
    fence = set_runtime_task_fence(task_id=run_id or n.run_id, worker_id=worker, claim_version=version)
    try:
        await asyncio.wait_for(execute_web_chat_run(run_id or n.run_id), timeout=60)
    finally:
        reset_runtime_task_fence(fence)
    return model


async def _wires(n, factory, run_id=None):
    from app.models.session_v2 import SessionModelResult

    async with tenant_scoped_session(n.tenant_id, session_factory=factory) as db:
        rows = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == (run_id or n.run_id))))
        return [row.model_request_snapshot_json["wire_request"] for row in rows]


async def test_csv_report_download_edit_and_continue_reaches_provider_and_wire(
    member_web, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.models.chat_artifact import ChatArtifact
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    n = member_web
    uploaded = await n.client.post("/chat/upload", data={"agent_id": str(n.agent_id), "skip_personal_kb": "true"},
        files={"file": ("sales.csv", b"product,sales\nAlpha,12\nBeta,30\n", "text/csv")})
    assert uploaded.status_code == 200, uploaded.text
    csv = uploaded.json()["workspace_path"]
    path, body = "workspace/sales-report.md", "# Sales\nTotal: 42\n"
    model = await _run(n, [
        {"tool_calls": [_call("read_file", "rl1-csv", path=csv)], "finish_reason": "tool_calls"},
        {"tool_calls": [_call("write_file", "rl1-report", path=path, content=body)], "finish_reason": "tool_calls"},
        {"content": "Report ready.\nDELIVERABLE: workspace/sales-report.md"},
    ], monkeypatch)
    assert "Alpha,12" in json.dumps(model.requests[1])
    assert "Alpha,12" in json.dumps(await _wires(n, app_user_sessionmaker))
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        assert (await db.get(RuntimeTask, n.run_id)).status == "completed"
        artifacts = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == n.run_id)))
        assert artifacts
        artifact = artifacts[-1]
    card = await n.client.get(f"/agents/{n.agent_id}/files/artifacts/{artifact.id}/download")
    original = await n.client.get(f"/agents/{n.agent_id}/files/download", params={"path": path})
    assert card.status_code == original.status_code == 200
    assert card.content == original.content == body.encode()
    human = "# Sales\nTotal: 42\nHuman correction: include returns.\n"
    saved = await n.client.put(f"/agents/{n.agent_id}/files/content", params={"path": path},
        json={"content": human, "expected_current_hash": hashlib.sha256(body.encode()).hexdigest()})
    assert saved.status_code == 200, saved.text
    conflict = await n.client.put(f"/agents/{n.agent_id}/files/content", params={"path": path},
        json={"content": "stale draft", "expected_current_hash": hashlib.sha256(body.encode()).hexdigest()})
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "workspace_content_conflict"
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=n.data_root)
    boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=n.tenant_id, worker_id="rl1-boundary", canonical_validator=processor.validate,
        process_callback=processor, task_types=("web_chat_turn",))
    assert boundary["delivered"] == 1, boundary
    continued = await n.client.post(f"/agents/{n.agent_id}/sessions/{n.session_id}/runs",
        json={"content": f"Continue my saved correction in {path}.", "input_id": str(uuid.uuid4()),
            "idempotency_key": f"rl1:{uuid.uuid4()}", "permission_mode": "bypassPermissions"})
    assert continued.status_code == 201, continued.text
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        runs = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.parent_session_id == str(n.session_id),
            RuntimeTask.id != n.run_id, RuntimeTask.task_type == "web_chat_turn")))
        assert len(runs) == 1
        run = runs[0]
        claims = await RuntimeTaskClaimService(db=db, worker_id="rl1-follow-up", lease_seconds=600).claim_available()
        claim = next(item for item in claims if item.id == run.id)
        version = claim.claim_version
        await db.commit()
    follower = await _run(n, [
        {"tool_calls": [_call("read_file", "rl1-human-read", path=path)], "finish_reason": "tool_calls"},
        {"tool_calls": [_call("edit_file", "rl1-human-edit", path=path, old_text="include returns.",
            new_text="include returns and refunds.")], "finish_reason": "tool_calls"},
        {"content": "Saved correction continued."},
    ], monkeypatch, run_id=run.id, worker="rl1-follow-up", version=version)
    assert "Human correction: include returns." in json.dumps(follower.requests[1])
    assert "Human correction: include returns." in json.dumps(await _wires(n, app_user_sessionmaker, run.id))
    assert (n.agent_root / path).read_text() == human.replace("include returns.", "include returns and refunds.")
    old_card = await n.client.get(f"/agents/{n.agent_id}/files/artifacts/{artifact.id}/download")
    assert old_card.status_code == 200 and old_card.content == body.encode()


@pytest.mark.parametrize("history", ["missing", "unverifiable"])
async def test_own_working_copy_with_unavailable_history_reaches_provider_and_wire(
    member_web, owner_sessionmaker, app_user_sessionmaker, monkeypatch, history,
):
    from tests.integration.test_artifact_publication_transactions import stage_report
    from tests.integration.test_r10a_workspace_source_admission import _unverifiable_history

    n = member_web
    async with owner_sessionmaker() as db:
        if history == "unverifiable":
            path, _ = await _unverifiable_history(db, tenant=n.tenant_id, agent=n.agent_id, user=n.user_id,
                session=n.session_id, root=n.agent_root)
        else:
            path = "workspace/earlier-report.md"
            await stage_report(db, tenant=n.tenant_id, agent=n.agent_id, user=n.user_id, session=n.session_id,
                root=n.agent_root, path=path, dependencies=[{"source_kind": "workspace_artifact",
                    "source_id": str(uuid.uuid4()), "source_version": "a" * 64}])
        await db.commit()
    expected = (n.agent_root / path).read_text()
    model = await _run(n, [
        {"tool_calls": [_call("read_file", "rl1-earlier-report", path=path)], "finish_reason": "tool_calls"},
        {"content": "Earlier report read."},
    ], monkeypatch)
    read = next(message for message in model.requests[1]["messages"] if message.get("tool_call_id") == "rl1-earlier-report")
    assert "COMPANY_SOURCE_MARKER" in read["content"], read
    assert expected in json.loads(read["content"])["content"], read
    assert any(expected in json.loads(message["content"])["content"]
        for wire in await _wires(n, app_user_sessionmaker) for message in wire["messages"]
        if message.get("tool_call_id") == "rl1-earlier-report")


async def test_shared_bot_denies_other_members_file_in_http_tool_provider_and_wire(
    member_web, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.models.user import User
    from app.models.chat_session import ChatSession
    from tests.integration.test_artifact_publication_transactions import publish_report, stage_report

    n = member_web
    path, secret = "workspace/other-member.md", "COMPANY_SOURCE_MARKER"
    async with owner_sessionmaker() as db:
        foreign = uuid.uuid4()
        db.add(User(id=foreign, tenant_id=n.tenant_id, username=f"rl1-{foreign.hex}",
            email=f"{foreign.hex}@fixture.test", password_hash="fixture", display_name="Other member", role="member"))
        await db.flush()
        foreign_session = uuid.uuid4()
        db.add(ChatSession(id=foreign_session, tenant_id=n.tenant_id, agent_id=n.agent_id, user_id=foreign))
        await db.flush()
        await stage_report(db, tenant=n.tenant_id, agent=n.agent_id, user=foreign, session=foreign_session,
            root=n.agent_root, path=path)
        await db.commit()
    publication = SimpleNamespace(tenant=n.tenant_id, agent=n.agent_id, users=[foreign],
        sessions=[foreign_session], paths=[path], root=n.agent_root)
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        parts, errors = await publish_report(db, publication, 0)
        await db.commit()
    assert not errors and parts
    for endpoint in ("content", "download"):
        denied = await n.client.get(f"/agents/{n.agent_id}/files/{endpoint}", params={"path": path})
        assert denied.status_code == 403 and secret not in denied.text, denied.text
    card = await n.client.get(f"/agents/{n.agent_id}/files/artifacts/{parts[0]['artifact_id']}/download")
    assert card.status_code == 403 and secret not in card.text, card.text
    model = await _run(n, [
        {"tool_calls": [_call("read_file", "rl1-foreign-file", path=path)], "finish_reason": "tool_calls"},
        {"content": "The file is private."},
    ], monkeypatch)
    assert "auth_or_permission" in json.dumps(model.requests[1])
    assert secret not in json.dumps(model.requests)
    assert secret not in json.dumps(await _wires(n, app_user_sessionmaker))


async def test_read_grant_does_not_allow_write_or_deliver(native_workspace, owner_sessionmaker, app_user_sessionmaker):  # noqa: F811
    from fastapi import HTTPException
    from app.core.resource_authority import authorize_resource_action, workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.models.user import User

    n = native_workspace
    async with owner_sessionmaker() as db:
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, n.path),
            actions=["read"], conditions={}))
        await db.commit()
    read, _ = await memory_call(n, app_user_sessionmaker, "read_file", {"path": n.path})
    assert "PRIVATE_FOREIGN_FILE_BODY" in read
    write = await n.client.put(f"/agents/{n.agent}/files/content", params={"path": n.path}, json={"content": "replacement"})
    assert write.status_code == 403
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        with pytest.raises(HTTPException) as denied:
            await authorize_resource_action(db, await db.get(User, n.user), agent_id=n.agent,
                resource_kind="workspace_file", resource_id=workspace_resource_id(n.agent, n.path),
                action="deliver", owner_user_id=n.foreign)
        assert denied.value.status_code == 403
    assert n.target.read_text() == "PRIVATE_FOREIGN_FILE_BODY"


async def test_tombstoned_object_is_refused_on_tool_and_browser(native_workspace, owner_sessionmaker):  # noqa: F811
    from app.models.workspace_resource import WorkspaceResourceManifest

    n = native_workspace
    path, secret = "workspace/deleted.md", "RL1_TOMBSTONED_BODY"
    saved = await n.client.put(f"/agents/{n.agent}/files/content", params={"path": path}, json={"content": secret})
    assert saved.status_code == 200, saved.text
    async with owner_sessionmaker() as db:
        row = await db.scalar(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.agent_id == n.agent,
            WorkspaceResourceManifest.path == path))
        row.deleted_at = datetime.now(timezone.utc)
        await db.commit()
    result = str(await n.service.execute("read_file", {"path": path}, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert '"error_class": "auth_or_permission"' in result and secret not in result
    for endpoint in ("content", "download"):
        denied = await n.client.get(f"/agents/{n.agent}/files/{endpoint}", params={"path": path})
        assert denied.status_code == 404 and secret not in denied.text
        assert denied.json()["detail"]["code"] == "workspace_resource_not_found"


async def test_card_download_and_read_file_statement_cost(native_workspace, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.api import upload
    from app.models.chat_artifact import ChatArtifact

    n = native_workspace
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", n.root.parent)
    n.client._transport.app.include_router(upload.router)
    uploaded = await n.client.post("/chat/upload", data={"agent_id": str(n.agent), "skip_personal_kb": "true"},
        files={"file": ("sales.csv", b"product,sales\nAlpha,12\nBeta,30\n", "text/csv")})
    assert uploaded.status_code == 200, uploaded.text
    csv = uploaded.json()["workspace_path"]
    read, _ = await memory_call(n, app_user_sessionmaker, "read_file", {"path": csv}, include_history=True)
    assert "Alpha,12" in read
    path, body = "workspace/cost-report.md", "RL1_COST_REPORT_42"
    await memory_call(n, app_user_sessionmaker, "write_file", {"path": path, "content": body}, include_history=True)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == n.run, ChatArtifact.path == path))
        assert artifact is not None
    with _profile(monkeypatch) as download_cost:
        response = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact.id}/download")
    assert response.status_code == 200 and response.content == body.encode(), response.text
    with _profile(monkeypatch) as read_cost:
        result = await n.service.execute("read_file", {"path": path}, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False)
    assert body in str(result), result
    print("RL1_STATEMENTS", json.dumps({"card_download": sum(row["statements"] for row in download_cost.values()),
        "read_file": sum(row["statements"] for row in read_cost.values())}), flush=True)


async def test_selected_unreceipted_read_grant_keeps_its_existing_source_boundary(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.models.session_v2 import SessionTurnInput
    from app.services.artifact_continuation import saved_file_context
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages

    n = native_workspace
    async with owner_sessionmaker() as db:
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, n.path),
            actions=["read"], conditions={}))
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        await _admitted_write_messages(db, n, content="Use the selected file.")
        row = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.target_run_id == n.run,
            SessionTurnInput.session_id == n.session, SessionTurnInput.status == "bound"))
        assert row is not None
        row.content_parts_json = [{"type": "workspace_file_ref", "agent_id": str(n.agent), "path": n.path,
            "human_saved_hash": hashlib.sha256(n.target.read_bytes()).hexdigest()}]
        await db.flush()
        messages, notices = await saved_file_context(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, run_id=n.run, user_id=n.user,
            messages=[{"session_input_id": str(row.id), "workspace_file_refs": True}])
        assert notices and messages
        assert "PRIVATE_FOREIGN_FILE_BODY" not in "\n".join(message.content for message in messages)
