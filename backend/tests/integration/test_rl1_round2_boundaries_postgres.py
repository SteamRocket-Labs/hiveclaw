"""Owned private copies stay usable without granting their sources to other readers."""

import hashlib
import json
import uuid
from types import SimpleNamespace
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.database import tenant_scoped_session
from tests.integration.test_rl1_member_files_without_replay_postgres import (
    _call, _run, _wires, member_web, native_workspace,  # noqa: F401
)
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_memory  # noqa: F401
from tests.integration.test_r19a_consumed_result import auxiliary_models  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _foreign_source(n, db):
    from app.core.resource_authority import workspace_resource_id
    from app.models.chat_session import ChatSession
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from tests.integration.test_artifact_publication_transactions import stage_report

    foreign, session = uuid.uuid4(), uuid.uuid4()
    db.add(User(id=foreign, tenant_id=n.tenant_id, username=foreign.hex, email=f"{foreign.hex}@fixture.test",
        password_hash="fixture", display_name="Source owner", role="member"))
    await db.flush()
    db.add(ChatSession(id=session, tenant_id=n.tenant_id, agent_id=n.agent_id, user_id=foreign))
    await db.flush()
    path = "workspace/private-source.md"
    receipt = await stage_report(db, tenant=n.tenant_id, agent=n.agent_id, user=foreign, session=session,
        root=n.agent_root, path=path)
    grant = ResourcePermission(tenant_id=n.tenant_id, principal_type="user", principal_id=n.user_id,
        resource_type="workspace_file", resource_id=workspace_resource_id(n.agent_id, path), actions=["read"], conditions={})
    db.add(grant)
    await db.flush()
    return SimpleNamespace(owner=foreign, session=session, path=path, grant=grant,
        dependency={"source_kind": "workspace_artifact", "source_id": str(receipt.id), "source_version": receipt.content_sha256})


@pytest.mark.parametrize("tool", ["read_file", "grep_search"])
async def test_grant_reader_cannot_send_revoked_private_source_to_provider_or_wire(
    member_web, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tool,  # noqa: F811
):
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from tests.integration.test_artifact_publication_transactions import stage_report

    n, path = member_web, "workspace/shared-report.md"
    async with owner_sessionmaker() as db:
        source = await _foreign_source(n, db)
        await stage_report(db, tenant=n.tenant_id, agent=n.agent_id, user=source.owner, session=source.session,
            root=n.agent_root, path=path, dependencies=[source.dependency])
        db.add(ResourcePermission(tenant_id=n.tenant_id, principal_type="user", principal_id=n.user_id,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent_id, path), actions=["read"], conditions={}))
        await db.delete(source.grant)
        await db.commit()
    args = {"path": path} if tool == "read_file" else {"pattern": "SOURCE", "root": path}
    model = await _run(n, [
        {"tool_calls": [_call(tool, "rl1-shared-read", **args)], "finish_reason": "tool_calls"},
        {"content": "The source is unavailable."},
    ], monkeypatch)
    read = next(m for m in model.requests[1]["messages"] if m.get("tool_call_id") == "rl1-shared-read")
    assert "COMPANY_SOURCE_MARKER" not in read["content"], read
    assert "COMPANY_SOURCE_MARKER" not in json.dumps(model.requests)
    assert "COMPANY_SOURCE_MARKER" not in json.dumps(await _wires(n, app_user_sessionmaker))


async def test_private_card_after_source_revocation_cannot_be_delivered_using_admin_inspection(
    member_web, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from fastapi import HTTPException

    from app.core.resource_authority import authorize_resource_action
    from app.core.source_read_purpose import source_recipient_read
    from app.models.chat_artifact import ChatArtifact
    from app.models.user import User
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    n, path = member_web, "workspace/new-admin-report.md"
    async with owner_sessionmaker() as db:
        source = await _foreign_source(n, db)
        (await db.get(User, n.user_id)).role = "org_admin"
        grant_id = source.grant.id
        await db.commit()
    stream = ScriptedLLMClient.stream

    async def revoke_before_new_write(self, **kwargs):
        if len(self.requests) == 1:
            from app.models.security_audit import ResourcePermission

            async with owner_sessionmaker() as db:
                await db.delete(await db.get(ResourcePermission, grant_id))
                await db.commit()
        return await stream(self, **kwargs)

    monkeypatch.setattr(ScriptedLLMClient, "stream", revoke_before_new_write)
    model = await _run(n, [
        {"tool_calls": [_call("read_file", "rl1-admin-source", path=source.path)], "finish_reason": "tool_calls"},
        {"tool_calls": [_call("write_file", "rl1-admin-report", path=path, content="COMPANY_SOURCE_MARKER")], "finish_reason": "tool_calls"},
        {"content": f"Prepared report.\nDELIVERABLE: {path}"},
    ], monkeypatch)
    assert "COMPANY_SOURCE_MARKER" in json.dumps(model.requests[1])
    assert (n.agent_root / path).read_text() == "COMPANY_SOURCE_MARKER"
    inspection = await n.client.get(f"/agents/{n.agent_id}/files/content", params={"path": source.path,
        "operator_view": True, "operator_reason": "Inspect the original source"})
    assert inspection.status_code == 200 and "COMPANY_SOURCE_MARKER" in inspection.text, inspection.text
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        artifacts = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == n.run_id,
            ChatArtifact.path == path)))
        # owner 11-18 decision A: the admin's own private card is available after revocation.
        assert artifacts
        user = await db.get(User, n.user_id)
        for artifact in artifacts:
            with source_recipient_read(session_id=n.session_id, user_id=n.user_id):
                with pytest.raises(HTTPException) as denied:
                    await authorize_resource_action(db, user, agent_id=n.agent_id, resource_kind="chat_artifact",
                        resource_id=artifact.id, action="deliver", owner_user_id=n.user_id,
                        root_session_id=n.session_id, authority_state=artifact.authority_state)
                assert denied.value.status_code == 403
    for artifact in artifacts:
        response = await n.client.get(f"/agents/{n.agent_id}/files/artifacts/{artifact.id}/download")
        assert response.status_code == 200 and response.content == b"COMPANY_SOURCE_MARKER", response.text


async def test_unknown_selected_sources_do_not_become_complete_empty_publish_inputs(
    member_web, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from fastapi import HTTPException

    from app.core.resource_authority import authorize_resource_action
    from app.models.agent import Agent
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_session import ChatSession
    from app.models.organization_identity import ContentInputReceipt
    from app.models.user import User
    from app.services.session_live_input import submit_live_human_input
    from tests.integration.test_artifact_publication_transactions import stage_report

    n, old, new = member_web, "workspace/old-bot-report.md", "workspace/new-derived-report.md"
    async with owner_sessionmaker() as db:
        source = await _foreign_source(n, db)
        receipt = await stage_report(db, tenant=n.tenant_id, agent=n.agent_id, user=n.user_id, session=n.session_id,
            root=n.agent_root, path=old, dependencies=[source.dependency])
        await db.flush()
        await db.execute(delete(ContentInputReceipt).where(ContentInputReceipt.id == receipt.id))
        await db.delete(source.grant)
        await db.commit()
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        await submit_live_human_input(db=db, agent=await db.get(Agent, n.agent_id), user=await db.get(User, n.user_id),
            session=await db.get(ChatSession, n.session_id), content="Use my selected saved report to write a new report.",
            parts=[{"type": "workspace_file_ref", "agent_id": str(n.agent_id), "path": old,
                "human_saved_hash": hashlib.sha256((n.agent_root / old).read_bytes()).hexdigest()}],
            source="web", source_channel="web", requested_kind="steer_current_turn",
            expected_turn_id=n.turn_id, expected_run_id=n.run_id)
        await db.commit()
    model = await _run(n, [
        {"tool_calls": [_call("write_file", "rl1-derived-write", path=new, content="COMPANY_SOURCE_MARKER")], "finish_reason": "tool_calls"},
        {"content": f"New report.\nDELIVERABLE: {new}"},
    ], monkeypatch)
    assert "COMPANY_SOURCE_MARKER" in json.dumps(model.requests[0])
    assert (n.agent_root / new).read_text() == "COMPANY_SOURCE_MARKER"
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        artifacts = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == n.run_id,
            ChatArtifact.path == new)))
        # owner 11-18 decision A: a local card preserves incomplete provenance and cannot be delivered.
        assert artifacts
        user = await db.get(User, n.user_id)
        for artifact in artifacts:
            with pytest.raises(HTTPException) as denied:
                await authorize_resource_action(db, user, agent_id=n.agent_id, resource_kind="chat_artifact",
                    resource_id=artifact.id, action="deliver", owner_user_id=n.user_id,
                    root_session_id=n.session_id, authority_state=artifact.authority_state)
            assert denied.value.detail["code"] == "learned_model_source_unavailable"
        from app.models.session_v2 import SessionModelResult

        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == n.run_id)))
        assert any(m.model_request_snapshot_json["input_bindings"]["dependency_coverage"] == {"complete": False}
            for m in models)
    for artifact in artifacts:
        response = await n.client.get(f"/agents/{n.agent_id}/files/artifacts/{artifact.id}/download")
        assert response.status_code == 200 and response.content == b"COMPANY_SOURCE_MARKER", response.text


@pytest.mark.parametrize("action", ["save", "upload", "tool_write", "history", "restore"])
async def test_deleted_path_keeps_write_upload_history_and_restore(
    native_workspace, app_user_sessionmaker, action,  # noqa: F811
):
    from app.api import files
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    n, path, body = native_workspace, "workspace/reusable.md", "RL1_RESTORABLE_BYTES"
    await memory_call(n, app_user_sessionmaker, "write_file", {"path": path, "content": body}, include_history=True)
    url = f"/agents/{n.agent}/files"
    before = await n.client.get(f"{url}/versions", params={"path": path})
    assert before.status_code == 200 and before.json()["versions"], before.text
    version = before.json()["versions"][0]["version_id"]
    removed = await n.client.delete(f"{url}/content", params={"path": path})
    assert removed.status_code == 200, removed.text
    if action == "save":
        response = await n.client.put(f"{url}/content", params={"path": path}, json={"content": "rewritten"})
    elif action == "upload":
        n.client._transport.app.include_router(files.upload_router)
        response = await n.client.post(f"{url}/upload", params={"path": "workspace/"},
            files={"file": ("reusable.md", b"uploaded", "text/markdown")})
    elif action == "tool_write":
        result, _ = await memory_call(n, app_user_sessionmaker, "write_file", {"path": path, "content": "tool rewritten"}, include_history=True)
        from app.services.content_input_receipts import latest_workspace_receipt

        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            receipt = await latest_workspace_receipt(db, n.tenant, n.agent, path)
            assert receipt.content_sha256 == hashlib.sha256(b"tool rewritten").hexdigest(), result
        assert (n.root / path).read_text() == "tool rewritten"
        return
    elif action == "restore":
        response = await n.client.post(f"{url}/versions/{version}/restore", params={"path": path},
            json={"expected_current_exists": False})
    else:
        response = await n.client.get(f"{url}/versions", params={"path": path})
        assert response.status_code == 200 and response.json()["versions"], response.text
        for endpoint in ("content", "download"):
            historical = await n.client.get(f"{url}/versions/{version}/{endpoint}", params={"path": path})
            assert historical.status_code == 200 and body in historical.text, historical.text
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("group", [False, True])
async def test_delivered_snapshot_survives_working_file_delete(
    group_memory, owner_sessionmaker, app_user_sessionmaker, group,  # noqa: F811
):
    from app.models.chat_artifact import ChatArtifact
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call
    from tests.integration.test_m4d_group_memory_postgres import group_call

    g, path, body = group_memory, "workspace/delivered.md", "RL1_DELIVERED_SNAPSHOT"
    n = g.n
    if group:
        await group_call(g, app_user_sessionmaker, "write_file", {"path": path, "content": body}, include_history=True)
    else:
        await memory_call(n, app_user_sessionmaker, "write_file", {"path": path, "content": body}, include_history=True)
    async with owner_sessionmaker() as db:
        artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == (g.runs[n.user] if group else n.run), ChatArtifact.path == path))
        assert artifact is not None
    deleted = await n.client.delete(f"/agents/{n.agent}/files/content", params={"path": path})
    assert deleted.status_code == 200, deleted.text
    response = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact.id}/download")
    assert response.status_code == 200 and response.content == body.encode(), response.text


@pytest.mark.parametrize("receipted,size", [(True, 70000), (False, 70000), (False, 32)])
async def test_selected_saved_version_checks_all_opened_bytes(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, receipted, size,  # noqa: F811
):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.session_v2 import SessionTurnInput
    from app.services.artifact_continuation import saved_file_context
    from app.services.content_input_receipts import workspace_artifact_ref
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages

    n, path = native_workspace, "workspace/large-saved.md"
    saved = await n.client.put(f"/agents/{n.agent}/files/content", params={"path": path}, json={"content": "a" * size})
    assert saved.status_code == 200, saved.text
    expected = saved.json()["content_hash"]
    (n.root / path).write_text("b" * size)
    async with owner_sessionmaker() as db:
        if not receipted:
            await db.execute(delete(ContentInputReceipt).where(ContentInputReceipt.carrier_ref == workspace_artifact_ref(n.agent, path)))
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        await _admitted_write_messages(db, n)
        row = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.target_run_id == n.run, SessionTurnInput.status == "bound"))
        row.content_parts_json = [{"type": "workspace_file_ref", "agent_id": str(n.agent), "path": path, "human_saved_hash": expected}]
        await db.flush()
        messages, notices = await saved_file_context(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, user_id=n.user, messages=[{"session_input_id": str(row.id), "workspace_file_refs": True}])
        assert notices and "文件已不是你保存时的版本" in messages[0].content
        assert "saved version was checked and is current" not in messages[0].content


@pytest.mark.usefixtures("auxiliary_models")
async def test_unpublishable_owned_chain_is_refused_when_colleague_result_returns_to_parent(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.config import get_settings
    from app.models.session_v2 import SessionModelResult
    from app.tools.service import ToolRuntimeService
    from tests.integration import test_r22a_owned_delegation_result_postgres as returns
    from tests.integration.test_r10a_workspace_source_admission import _unverifiable_history
    from app.services.content_input_receipts import latest_workspace_receipt

    execute = ToolRuntimeService.execute
    observed = []

    async def seed_owned_chain(self, tool_name, arguments, **kwargs):
        if tool_name == "read_file" and kwargs.get("tool_call_id") == "child-file":
            async with owner_sessionmaker() as db:
                tenant = (await db.get(returns.Agent, kwargs["agent_id"])).tenant_id
                path, _ = await _unverifiable_history(db, tenant=tenant,
                    agent=kwargs["agent_id"], user=kwargs["user_id"], session=uuid.UUID(kwargs["session_id"]),
                    root=Path(get_settings().AGENT_DATA_DIR) / str(kwargs["agent_id"]))
                source = await latest_workspace_receipt(db, tenant, kwargs["agent_id"], path)
                receipt = await latest_workspace_receipt(db, tenant, kwargs["agent_id"], arguments["path"])
                receipt.dependencies_json = [*receipt.dependencies_json, {"source_kind": "workspace_artifact",
                    "source_id": str(source.id), "source_version": source.content_sha256}]
                await db.commit()
        result = await execute(self, tool_name, arguments, **kwargs)
        if kwargs.get("tool_call_id") == "child-file":
            observed.append(str(result))
        return result

    monkeypatch.setattr(returns, "CSV", returns.CSV + "COMPANY_SOURCE_MARKER\n")
    monkeypatch.setattr(ToolRuntimeService, "execute", seed_owned_chain)
    seed = await returns._file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    assert observed and "COMPANY_SOURCE_MARKER" in observed[0]
    assert "COMPANY_SOURCE_MARKER" in json.dumps(seed["child_client"].requests[-1])
    run_id, parent = await returns._parent_turn(owner_sessionmaker, seed, monkeypatch, [
        {"tool_calls": [_call("read_runtime_result", "rl1-parent-result", result_ref=seed["result_ref"])], "finish_reason": "tool_calls"},
        {"content": "The colleague result needs authorized sources."},
    ])
    wire = json.dumps(parent.requests)
    assert returns.RESULT not in wire and "COMPANY_SOURCE_MARKER" not in wire
    async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)))
        assert models
        for model in models:
            wire = json.dumps(model.model_request_snapshot_json["wire_request"])
            assert returns.RESULT not in wire and "COMPANY_SOURCE_MARKER" not in wire
