"""Collected native read tools consume explicit originals with current authority."""

import base64
import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.services.personal_knowledge_access import AgentRuntimePrincipal
from app.services.workspace_resource_authority import load_workspace_authority_scope
from app.tools.collector import collect_tools
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
from tests.integration.test_m4v_upload_intents_postgres import provider, original_api, delivered, s3_server  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def payload(kind):
    if kind == "image":
        return "source.png", base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+lmJkAAAAASUVORK5CYII=")
    if kind == "document":
        from docx import Document

        document = Document()
        document.add_paragraph("Native original source.")
        stream = io.BytesIO()
        document.save(stream)
        return "source.docx", stream.getvalue()
    return "source.txt", b"Native original source."


async def context(d, factory, user_id=None):
    from app.models.user import User

    identity = user_id or d.user.id
    async with factory() as db:
        await pin_rls_tenant_context(db, d.tenant)
        user = await db.get(User, identity)
        scope = await load_workspace_authority_scope(db, user, agent_id=d.agent,
            session_id=d.session if identity == d.user.id else None,
            runtime_principal=AgentRuntimePrincipal(agent_id=d.agent, requester_user_id=identity))
        await db.commit()
    return ToolExecutionContext(agent_id=d.agent, user_id=identity, tenant_id=str(d.tenant),
        workspace=d.root, session_id=str(d.session) if identity == d.user.id else None, workspace_authority_scope=scope)


async def read(frame, row, tool="fs_read", kind="text", **extra):
    arguments = {"path": row["workspace_path"], "original_attachment_id": row["original_attachment"]["id"], **extra}
    if tool == "fs_read":
        arguments["mode"] = "document" if kind == "document" else "text"
    return await collect_tools().exec_registry.try_execute(ToolExecutionRequest(tool_name=tool, arguments=arguments, context=frame))


@pytest.mark.parametrize("tool,kind", [("read_file", "text"), ("fs_read", "text"),
    ("read_file", "image"), ("fs_read", "image"), ("read_document", "document"), ("fs_read", "document")])
async def test_collected_read_tools_choose_verified_original_without_work_copy(original_api, provider, app_user_sessionmaker, tool, kind):  # noqa: F811
    from app.tools.result_envelope import ToolContentEnvelope

    d, client = original_api
    filename, body = payload(kind)
    response = await client.post("/chat/upload", files={"file": (filename, body)},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert response.status_code == 200, response.text
    row = response.json()
    frame = await context(d, app_user_sessionmaker)
    work = d.root / row["workspace_path"]
    work.write_bytes(b"EDITED_WORK_COPY")
    result = await read(frame, row, tool, kind)
    if kind == "image":
        assert isinstance(result, ToolContentEnvelope)
        assert result.blocks[0].type == "image" and base64.b64decode(result.blocks[0].data) == body
    else:
        assert "Native original source." in str(result) and "EDITED_WORK_COPY" not in str(result), str(result)
    assert row["original_attachment"]["id"] in str(result)
    assert row["original_attachment"]["content_sha256"] in str(result)
    assert work.read_bytes() == b"EDITED_WORK_COPY"
    work.unlink()
    repeated = await read(frame, row, tool, kind)
    assert str(repeated) == str(result)
    if kind == "image":
        assert repeated.blocks == result.blocks
    assert not work.exists()
    assert not list(Path(d.root).rglob("document_sources"))


async def test_original_grant_is_separate_from_copy_and_rechecked_on_every_read(original_api, provider, app_user_sessionmaker, owner_sessionmaker):  # noqa: F811
    from app.core.resource_authority import workspace_resource_id
    from app.models.agent import AgentPermission
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.services.content_input_receipts import ContentInputReceiptError, validate_browser_input_dependencies

    d, client = original_api
    response = await client.post("/chat/upload", files={"file": ("source.txt", b"PRIVATE_ORIGINAL")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert response.status_code == 200, response.text
    row = response.json()
    other = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=other, tenant_id=d.tenant, username=str(other), email=f"{other}@fixture.test",
            display_name="Other reader", password_hash="fixture", role="member", is_active=True))
        db.add(AgentPermission(tenant_id=d.tenant, agent_id=d.agent, scope_type="user", scope_id=other, access_level="use"))
        db.add(ResourcePermission(tenant_id=d.tenant, principal_type="user", principal_id=other,
            resource_type="workspace_file", resource_id=workspace_resource_id(d.agent, row["workspace_path"]),
            actions=["read"], effect="allow", conditions={}))
        await db.commit()
    frame = await context(d, app_user_sessionmaker, other)
    (d.root / row["workspace_path"]).write_bytes(b"SHARED_WORK_COPY")
    normal = await collect_tools().exec_registry.try_execute(ToolExecutionRequest(tool_name="fs_read",
        arguments={"path": row["workspace_path"]}, context=frame))
    assert "SHARED_WORK_COPY" in str(normal)
    before = len(provider[1].requests)
    denied = await read(frame, row, execution_context={"user_id": str(d.user.id), "tenant_id": str(d.tenant)})
    assert "auth_or_permission" in str(denied) and "PRIVATE_ORIGINAL" not in str(denied)
    assert len(provider[1].requests) == before
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        grant = ResourcePermission(tenant_id=d.tenant, principal_type="user", principal_id=other,
            resource_type="upload_attachment", resource_id=uuid.UUID(row["original_attachment"]["id"]),
            actions=["read"], effect="allow", conditions={})
        db.add(grant)
        await db.commit()
        grant_id = grant.id
    assert "PRIVATE_ORIGINAL" in str(await read(frame, row))
    dependency = {"source_kind": "original_attachment", "source_id": row["original_attachment"]["id"],
        "source_version": row["original_attachment"]["content_sha256"]}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        await validate_browser_input_dependencies(db, d.tenant, await db.get(User, other), [dependency])
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        (await db.get(ResourcePermission, grant_id)).revoked_at = datetime.now(timezone.utc)
        await db.commit()
    before = len(provider[1].requests)
    revoked = await read(frame, row)
    assert "auth_or_permission" in str(revoked) and "PRIVATE_ORIGINAL" not in str(revoked)
    assert len(provider[1].requests) == before
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        with pytest.raises(ContentInputReceiptError) as revoked_source:
            await validate_browser_input_dependencies(db, d.tenant, await db.get(User, other), [dependency])
        assert revoked_source.value.status_code == 403


@pytest.mark.parametrize("damage", ["malformed", "wrong_path", "released", "corrupt", "inactive"])
async def test_original_failure_never_returns_work_copy(original_api, provider, app_user_sessionmaker, damage):  # noqa: F811
    from app.models.storage_blob import StorageBlobRef, StorageBlob
    from app.models.user import User

    d, client = original_api
    response = await client.post("/chat/upload", files={"file": ("source.txt", b"PRIVATE_ORIGINAL")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert response.status_code == 200, response.text
    row = response.json()
    frame = await context(d, app_user_sessionmaker)
    work = d.root / row["workspace_path"]
    work.write_bytes(b"EDITED_WORK_COPY")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        ref = await db.get(StorageBlobRef, uuid.UUID(row["original_attachment"]["id"]))
        if damage == "malformed":
            row["original_attachment"]["id"] = "unrecorded-original"
        elif damage == "wrong_path":
            row["workspace_path"] = "workspace/report.txt"
        elif damage == "released":
            await db.delete(ref)
        elif damage == "inactive":
            (await db.get(User, d.user.id)).is_active = False
        elif provider[0] == "s3":
            provider[1].mode = "corrupt"
        else:
            blob = await db.get(StorageBlob, ref.blob_id)
            from app.services.upload_originals import _recorded_store

            store = _recorded_store(blob, d.tenant)
            # The exact owned fixture object is intentionally damaged.
            object_path = store.root / blob.bucket / blob.object_key
            object_path.write_bytes(b"CORRUPT_ORIGINAL")
        await db.commit()
    result = await read(frame, row)
    assert "<tool_error>" in str(result), str(result)
    assert "PRIVATE_ORIGINAL" not in str(result) and "EDITED_WORK_COPY" not in str(result)
    assert work.read_bytes() == b"EDITED_WORK_COPY"


async def test_scheduled_effect_rechecks_the_original_leaf_after_reader_revocation(original_api, provider, app_user_sessionmaker):  # noqa: F811
    from app.kernel.contracts import ExecutionAuthorityUnavailable
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services.trigger_read_sources import validate_native_trigger_model_sources

    d, client = original_api
    response = await client.post("/chat/upload", files={"file": ("source.txt", b"PRIVATE_ORIGINAL")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert response.status_code == 200, response.text
    row = response.json()
    dependency = {"source_kind": "original_attachment", "source_id": row["original_attachment"]["id"],
        "source_version": row["original_attachment"]["content_sha256"]}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        task = await db.get(RuntimeTask, d.run)
        # Exercise the scheduled pre-effect leaf validator under a real
        # persisted private task/session, without claiming a scheduler run.
        task.metadata_json = {**task.metadata_json, "trigger_origin": {}}
        await validate_native_trigger_model_sources(db, task=task, dependencies=[dependency], effect="tool_execution")
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        (await db.get(User, d.user.id)).is_active = False
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        with pytest.raises(ExecutionAuthorityUnavailable) as denied:
            await validate_native_trigger_model_sources(db, task=await db.get(RuntimeTask, d.run),
                dependencies=[dependency], effect="tool_execution")
        assert denied.value.code == "content_source_access_denied"


async def test_existing_image_limit_returns_source_free_failure_for_native_recovery(original_api, provider, app_user_sessionmaker):  # noqa: F811
    from app.services.agent_tool_domains.workspace import _MAX_IMAGE_BYTES

    d, client = original_api
    filename, image = payload("image")
    body = image + bytes(_MAX_IMAGE_BYTES + 1 - len(image))
    response = await client.post("/chat/upload", files={"file": (filename, body)},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert response.status_code == 200, response.text
    row = response.json()
    frame = await context(d, app_user_sessionmaker)
    before = len(provider[1].requests)
    result = await read(frame, row)
    assert "Image too large" in str(result) and "<tool_error>" in str(result)
    assert result.blocks == ()
    assert result.metadata.get("original_read_status") == "unavailable"
    assert "original_attachment" not in result.metadata
    assert len(provider[1].requests) == before


@pytest.mark.parametrize("first_failure", [False, True, "formatter", "marker"])
async def test_actual_native_worker_consumes_original_with_complete_sources_and_retry(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, first_failure,  # noqa: F811
):
    import app.runtime.invoker as invoker
    from app.api import upload
    from app.config import get_settings
    from app.core.security import create_access_token
    from app.models.agent import Agent
    from app.models.invocation_span import InvocationSpan
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionTurnInput
    from app.models.storage_blob import StorageBlobRef
    from app.models.user import User
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.secrets_provider import get_secrets_provider
    from app.services.session_human_input import revise_unbound_human_input
    from app.services.session_v2_persistence import resolve_session_mutation_authority
    from app.services.web_chat_runtime import execute_web_chat_run
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run, ScriptedLLMClient

    _, client = original_api
    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="Read the uploaded original and report its exact content.")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_manager, "settings", get_settings())
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        await AgentManager().initialize_agent_files(db, agent, personality="Read authorized uploaded sources.", original_author_id=seed["user_id"])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-original-provider-only")
        model.max_input_tokens = 256_000
        await db.commit()
    body = b"Native original source."
    if first_failure == "marker":
        body += b'\n<tool_error>{"ok":false,"error_class":"operation_failed","message":"SOURCE_DATA_NOT_AN_EXECUTOR_ERROR"}</tool_error>'
    needs_retry = first_failure is True or first_failure == "formatter"
    response = await client.post("/chat/upload", headers={"Authorization": "Bearer " + create_access_token(
        str(seed["user_id"]), "member", str(seed["tenant_id"]))}, files={"file": ("source.txt", body)},
        data={"agent_id": str(seed["agent_id"]), "skip_personal_kb": "true"})
    assert response.status_code == 200, response.text
    row = response.json()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        authority = await resolve_session_mutation_authority(db, user=await db.get(User, seed["user_id"]),
            agent_id=seed["agent_id"], session_id=seed["session_id"], action="mutate_session_input")
        pending = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.session_id == seed["session_id"]))
        await revise_unbound_human_input(db, authority=authority, input_id=pending.id, expected_revision=1,
            content_parts=[*pending.content_parts_json, {"type": "attachment_ref", "attachment": {
                "name": row["filename"], "path": row["workspace_path"], "original_attachment": row["original_attachment"]}}],
            user=await db.get(User, seed["user_id"]))
        await db.commit()
    work = seed["agent_root"] / row["workspace_path"]
    work.unlink()
    arguments = {"path": row["workspace_path"], "original_attachment_id": row["original_attachment"]["id"]}
    script = [{"tool_calls": [{"id": "original-read", "type": "function", "function": {"name": "fs_read",
        "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"}, {"content": "Native original source.", "finish_reason": "stop"}]
    if first_failure is True:
        script.insert(0, {"tool_calls": [{"id": "invalid-original-read", "type": "function", "function": {"name": "fs_read",
            "arguments": json.dumps({**arguments, "original_attachment_id": str(uuid.uuid4())})}}], "finish_reason": "tool_calls"})
    elif first_failure == "formatter":
        import app.services.agent_tool_domains.workspace as workspace_domain
        from contextlib import contextmanager

        open_file = workspace_domain.open_workspace_file
        failed = False

        @contextmanager
        def temporary_read_failure(root, path, *args, **kwargs):
            nonlocal failed
            if path == "original.txt" and not failed:
                failed = True
                raise OSError("Synthetic formatter read failure after verified capture")
            with open_file(root, path, *args, **kwargs) as stream:
                yield stream

        monkeypatch.setattr(workspace_domain, "open_workspace_file", temporary_read_failure)
        script.insert(0, {"tool_calls": [{"id": "original-format-error", "type": "function", "function": {"name": "fs_read",
            "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"})
    model_client = ScriptedLLMClient(script)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model_client)
    fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(seed["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    (tmp_path / "original-native-model-requests.json").write_text(json.dumps(model_client.requests, default=str, indent=2))
    assert len(model_client.requests) == (3 if needs_retry else 2)
    messages = model_client.requests[-1]["messages"]
    tool_contents = [str(value["content"]) for value in messages if value.get("role") == "tool"]
    assert any("Native original source." in value and row["original_attachment"]["content_sha256"] in value for value in tool_contents)
    if first_failure == "marker":
        assert any(json.loads(value)["content"] == body.decode() for value in tool_contents)
    assert not work.exists()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        assert (await db.get(RuntimeTask, seed["run_id"])).status == "completed"
        results = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))).all()
        assert all(value.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for value in results)
        dependencies = [source for value in results for source in value.model_request_snapshot_json["input_bindings"]["dependencies"]]
        assert {"source_kind": "original_attachment", "source_id": row["original_attachment"]["id"],
            "source_version": row["original_attachment"]["content_sha256"]} in dependencies
        pins = (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.tenant_id == seed["tenant_id"],
            StorageBlobRef.owner_type == "session_tool_read"))).all()
        assert len(pins) == 1 and pins[0].metadata_json["source_ref_id"] == row["original_attachment"]["id"]
        proofs = (await db.scalars(select(InvocationSpan).where(InvocationSpan.runtime_task_id == seed["run_id"],
            InvocationSpan.span_type == "original_attachment_read"))).all()
        assert len(proofs) == (2 if needs_retry else 1)
        if needs_retry:
            failures = [value.metadata_json["original_read"] for value in proofs
                if value.metadata_json["original_read"]["status"] != "verified"]
            assert len(failures) == 1 and failures[0]["sources"] == []
    if first_failure is False and provider[0] == "filesystem":
        from app.api import chat_sessions

        client._transport.app.include_router(chat_sessions.router)
        deleted = await client.delete(f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}", headers={
            "Authorization": "Bearer " + create_access_token(str(seed["user_id"]), "member", str(seed["tenant_id"]))})
        assert deleted.status_code == 204, deleted.text
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            closed = (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.tenant_id == seed["tenant_id"],
                StorageBlobRef.owner_type.in_(("session_input", "session_tool_read"))))).all()
            assert {pin.owner_type for pin in closed} == {"session_input", "session_tool_read"}
            assert all(pin.metadata_json.get("session_retirement", {}).get("schema") == "hive.original_session_retirement.v1" for pin in closed)
