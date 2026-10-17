"""Artifact publication cannot turn a failed or write-only tool into a read grant."""

import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def native_workspace(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    from app.config import get_settings
    from app.models.user import User
    from app.models.capability_policy import CapabilityPolicy
    from app.services import agent_tools
    from app.services.workspace_resource_authority import register_workspace_path

    tenant, user, agent, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("app.tools.workspace.async_session", app_user_sessionmaker)
    path = "workspace/private-report.md"
    root = tmp_path / str(agent)
    target = root / path
    target.parent.mkdir(parents=True)
    target.write_text("PRIVATE_FOREIGN_FILE_BODY")
    foreign = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=foreign, tenant_id=tenant, username=f"publisher-{foreign.hex}",
            email=f"{foreign.hex}@fixture.test", password_hash="fixture", display_name="Other owner", role="member"))
        (await db.get(User, user)).role = "member"
        db.add(CapabilityPolicy(tenant_id=tenant, capability="workspace.file.write", allowed=True,
            requires_approval=False))
        await db.flush()
        await register_workspace_path(db, tenant_id=tenant, agent_id=agent, path=path,
            owner_user_id=foreign, root_session_id=None, source="fixture",
            content_hash=hashlib.sha256(target.read_bytes()).hexdigest())
        await db.commit()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api import files
    from app.core.security import get_current_user
    from app.database import get_db

    async def current_user():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            return await db.get(User, user)

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = scoped_db
    monkeypatch.setattr(files, "_load_download_user_from_jwt", lambda **_kwargs: current_user())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local",
        headers={"Authorization": "Bearer fixture-identity"}) as client:
        yield SimpleNamespace(tenant=tenant, user=user, agent=agent, session=session, run=run,
            path=path, root=root, target=target, foreign=foreign, service=agent_tools._get_tool_runtime_service(), client=client)


async def _prepare_write(db, n, *, messages=None):
    from app.services.session_model_round import prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_tool_runtime import prepare_tool_invocation

    arguments = {"path": n.path, "content": "Requester's authorized replacement"}
    tools = [{"type": "function", "function": {"name": "write_file"}}]
    turn, call = f"turn-{n.run.hex}", f"write-{n.run.hex}"
    request_id = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
        run_id=n.run, turn_id=turn, round_index=1, messages=messages if messages is not None else [{"role": "user", "content": "Write the report"}],
        tools=tools, provider="openai", model="synthetic-no-provider")
    await seal_model_response(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
        run_id=n.run, turn_id=turn, round_index=1, provider_request_id=request_id,
        response={"content": None, "tool_calls": [{"id": call, "type": "function", "function": {
            "name": "write_file", "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    await commit_sealed_model_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
        run_id=n.run, turn_id=turn, round_index=1, provider_request_id=request_id)
    invocation = await prepare_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
        run_id=n.run, provider_request_id=request_id, provider_tool_use_id=call, tool_name="write_file", arguments=arguments)
    await db.commit()
    return invocation, arguments, turn


async def _admitted_write_messages(db, n):
    from app.kernel.engine import _mid_run_items_to_user_messages
    from app.runtime.hooks import HookResult
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_model_round import bind_round_inputs
    from tests.services.test_session_input_control_v2 import _accepted_input, _authority

    async def allow(**_kwargs):
        return HookResult()

    authority = await _authority(db, user_id=n.user, agent_id=n.agent, session_id=n.session)
    admitted = await _accepted_input(db, authority=authority, session_id=n.session, run_id=n.run,
        kind="steer_current_turn", content="Write the report")
    await run_user_prompt_admission(db, authority=authority, input_id=admitted.input_id,
        worker_id="native-output-control", hook_executor=allow)
    await queue_admitted_human_input(db, authority=authority, input_id=admitted.input_id)
    bound = await bind_round_inputs(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
        run_id=n.run, turn_id=f"turn-{n.run.hex}", round_index=1)
    return _mid_run_items_to_user_messages(bound)


@pytest.mark.parametrize("actions,publication_case", [
    ([], "normal"), (["write"], "normal"), (["read", "write"], "normal"),
    (["read", "write"], "revoked"), (["read", "write"], "capture_failed"),
])
async def test_native_callback_only_publishes_authorized_successful_file_versions(
    native_workspace, app_user_sessionmaker, monkeypatch, actions, publication_case,
):
    from app.core.resource_authority import workspace_resource_id
    from app.models.chat_artifact import ChatArtifact
    from app.models.security_audit import ResourcePermission
    from app.models.session_v2 import SessionToolInvocation
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call

    n = native_workspace
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        if actions:
            grant = ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
                resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, n.path),
                actions=actions, conditions={})
            db.add(grant)
            await db.commit()
        if "read" in actions:
            from app.services.content_input_receipts import record_workspace_receipt

            assert n.target.read_bytes() == b"PRIVATE_FOREIGN_FILE_BODY"
            record_workspace_receipt(db, n.tenant, n.agent, n.path,
                hashlib.sha256(b"PRIVATE_FOREIGN_FILE_BODY").hexdigest(), None)
        invocation, arguments, turn = await _prepare_write(db, n, messages=await _admitted_write_messages(db, n))
        trace = {}
        raw = await n.service.execute("write_file", arguments, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
            tool_call_id=invocation.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace)
        assert "PRIVATE_FOREIGN_FILE_BODY" not in str(raw)
        assert n.target.read_text() == (arguments["content"] if actions else "PRIVATE_FOREIGN_FILE_BODY"), raw
        if publication_case == "revoked":
            grant.actions = ["write"]
            await db.commit()
        if publication_case == "capture_failed":
            def fail_capture(**_kwargs):
                raise OSError("Fixture snapshot storage unavailable")

            monkeypatch.setattr("app.services.chat_artifact_delivery.build_artifact_candidate", fail_capture)
        envelopes = await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session),
            data={"name": "write_file", "args": arguments, "status": "done", "result": str(raw),
                "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
                "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
        assert "PRIVATE_FOREIGN_FILE_BODY" not in json.dumps(envelopes), envelopes
        rows = (await db.scalars(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent))).all()
        if "read" in actions and publication_case == "normal":
            assert len(rows) == 1
            assert rows[0].snapshot_json["content_hash"] == hashlib.sha256(arguments["content"].encode()).hexdigest()
            assert rows[0].snapshot_json["tool_call_id"] == invocation.provider_tool_use_id
            url = f"/agents/{n.agent}/files/artifacts/{rows[0].id}"
            for endpoint in ("/content", "/download"):
                response = await n.client.get(url + endpoint)
                assert response.status_code == 200, response.text
            grant.actions = ["write"]
            await db.commit()
            from app.runtime.a2a_workflow import A2AOutputArtifact
            from app.services.a2a_workflow_artifacts import A2AArtifactUnavailable, load_artifact_packet

            async def load_packet():
                return await load_artifact_packet(db, tenant_id=n.tenant, requester_user_id=n.user,
                    run_id=n.run, node_id="producer", producer_agent_id=n.agent, producer_session_id=n.session,
                    producer_runtime_task_id=n.run, output=A2AOutputArtifact(name="report", path=n.path), max_bytes=1000)

            with pytest.raises(A2AArtifactUnavailable, match="artifact_source_access_denied"):
                await load_packet()
            for endpoint in ("/content", "/download"):
                response = await n.client.get(url + endpoint)
                assert response.status_code == 403, response.text
                assert arguments["content"] not in response.text
            grant.actions = actions
            await db.commit()
            assert (await n.client.get(url + "/content")).status_code == 200
            assert (await load_packet())["content"] == arguments["content"]
        else:
            assert not rows
        await db.refresh(invocation)
        assert invocation.effect_state == ("effect_committed" if actions else "prepared_not_started")
        if not actions:
            assert any(item["kind"] == "tool_call.denied" for item in envelopes)
        if actions == ["write"] or publication_case != "normal":
            results = [item["payload"] for item in envelopes if item["item_kind"] == "tool_result"]
            delivery = results[0]["metadata"]["artifact_delivery"]
            assert delivery["status"] == ("unknown" if publication_case == "capture_failed" else "denied")
            assert results[0]["outcome"] == "success"
            assert delivery["files"][0]["retryable"] is False
        assert await db.get(SessionToolInvocation, invocation.id) is invocation


async def test_bound_bridge_upload_registers_its_source_before_artifact_publication(
    native_workspace, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from io import BytesIO
    from starlette.datastructures import UploadFile
    from app.api import local_bridge, upload
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.local_bridge_service import BridgeAuthContext

    n = native_workspace
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(local_bridge, "WORKSPACE_ROOT", tmp_path)
    content = b"Bound bridge fixture upload\n"
    context = BridgeAuthContext(connection_id=uuid.uuid4(), tenant_id=n.tenant, agent_id=n.agent,
        user_id=n.user, scopes=("files:upload",), client_kind="generic_mcp_stdio", device_name="Fixture")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        reply = await local_bridge.save_bridge_upload(
            file=UploadFile(BytesIO(content), filename="bridge-report.txt"), context=context, db=db)
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == reply["workspace_path"]))
        assert manifest.owner_user_id == n.user and manifest.authority_state == "owned"
        assert manifest.content_hash == hashlib.sha256(content).hexdigest()
        assert len(reply["artifacts"]) == 1
    artifact_id = reply["artifacts"][0]["artifact_id"]
    response = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact_id}/content")
    assert response.status_code == 200, response.text
    assert response.json()["content"].encode() == content
