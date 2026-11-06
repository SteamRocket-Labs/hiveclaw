"""Approved work and owned working copies stay usable without bootstrap receipts."""
import uuid
from pathlib import Path

import pytest

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_session_permission_execution_once import _mounted_client
from tests.services.test_session_permission_runtime import _seed_waiting_batch, _install_successful_executor


async def test_approved_effect_with_current_soul_executes_once(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.config import get_settings
    from app.database import pin_rls_tenant_context
    from app.models.session_v2 import SessionToolInvocation
    from app.services import delegation_permission_runtime, runtime_task_worker
    from app.services.agent_context import load_agent_context_resource

    seed = await _seed_waiting_batch(owner_sessionmaker, tool_name="send_email", tool_arguments={"to": "local@example.test"})
    root = Path(get_settings().AGENT_DATA_DIR) / str(seed.agent_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "soul.md").write_text("# Personality\nHelp with internal work.\n")
    resource = await load_agent_context_resource(agent_id=seed.agent_id, tenant_id=seed.tenant_id, resource_ref="soul")
    dependency = {"source_kind": "agent_context_resource", "source_id": f"{seed.agent_id}\nsoul", "source_version": resource.sha256}
    monkeypatch.setattr(delegation_permission_runtime, "_model_dependencies", lambda _: ([dependency], True))
    calls = _install_successful_executor(monkeypatch, result="Message delivered by local test transport")

    async def notify(**kwargs):
        pass

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    path = f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}/permissions/{seed.permission_ids[0]}/resolve"
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        response = await client.post(path, json={"action": "allow_once"})
        assert response.status_code == 200, response.text
        replay = await client.post(path, json={"action": "allow_once"})
        assert replay.status_code == 200, replay.text
        assert replay.json()["result_event_id"] == response.json()["result_event_id"]
        assert response.json()["run_status"] == "resumable"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        assert invocation.effect_state == "effect_committed"
    assert calls == [str(seed.run_id)]


@pytest.mark.parametrize("reader", ["read_file", "fs_read", "read_document"])
async def test_owned_file_written_read_searched_and_edited_across_turns(native_workspace, reader, owner_sessionmaker):  # noqa: F811
    n = native_workspace
    from app.models.capability_policy import CapabilityPolicy

    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="workspace.file.read", allowed=True, requires_approval=False))
        await db.commit()
    path = "workspace/current-sales.md"
    async def execute(tool, args, turn):
        return str(await n.service.execute(tool, args, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
            tool_call_id=str(uuid.uuid4()), emit_runtime_hooks=False))

    result = await execute("write_file", {"path": path, "content": "Sales total: 42\n"}, "turn-one")
    assert "Written" in result, result
    for turn, old, new in (("turn-one", "42", "43"), ("turn-two", "43", "44")):
        result = await execute(reader, {"path": path}, turn)
        assert f"Sales total: {old}" in result, result
        result = await execute("grep_search", {"pattern": "Sales", "root": "workspace"}, turn)
        assert f"Sales total: {old}" in result and path in result, result
        result = await execute("edit_file", {"path": path, "old_text": old, "new_text": new}, turn)
        assert "Updated" in result, result
    assert (n.root / path).read_text() == "Sales total: 44\n"


async def test_external_email_asks_then_executes_once_in_place(native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from types import SimpleNamespace
    from app.database import pin_rls_tenant_context
    from app.models.session_v2 import SessionToolInvocation
    from app.services import agent_tools, runtime_task_worker
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import complete_tool_invocation
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _prepare_write

    n = native_workspace
    arguments = {"to": "recipient@example.test", "subject": "Test", "body": "Approved local delivery"}
    async with owner_sessionmaker() as db:
        invocation, arguments, turn = await _prepare_write(db, n, tool_name="send_email", arguments=arguments)
        invocation_id, call_id = invocation.id, invocation.provider_tool_use_id
    deliveries = []

    async def deliver(tool_name, agent_id, workspace, args, **kwargs):
        deliveries.append(dict(args))
        return "Email sent successfully by the local test transport"

    async def notify(**kwargs):
        pass

    monkeypatch.setattr(agent_tools, "_handle_email_tool", deliver)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    trace = {}
    result = await n.service.execute("send_email", arguments, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn, tool_call_id=call_id,
        trace_metadata_sink=trace, emit_runtime_hooks=False)
    assert "session_permission_required" in str(result), result
    assert deliveries == []
    async with owner_sessionmaker() as db:
        await complete_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            invocation_id=invocation_id, provider_result_content=str(result), execution_evidence=_execution_evidence(trace))
        await db.commit()
        invocation = await db.get(SessionToolInvocation, invocation_id)
        permission_id = invocation.permission_item_id
    seed = SimpleNamespace(user_id=n.user, tenant_id=n.tenant)
    path = f"/api/agents/{n.agent}/sessions/{n.session}/permissions/{permission_id}/resolve"
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        first = await client.post(path, json={"action": "allow_once"})
        assert first.status_code == 200, first.text
        replay = await client.post(path, json={"action": "allow_once"})
        assert replay.status_code == 200, replay.text
        assert replay.json()["result_event_id"] == first.json()["result_event_id"]
        assert first.json()["run_id"] == str(n.run) and first.json()["run_status"] == "resumable"
    assert deliveries == [arguments]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        invocation = await db.get(SessionToolInvocation, invocation_id)
        assert invocation.effect_state == "effect_committed"


async def test_member_can_edit_and_read_own_quarantined_copy_but_not_another_members_file(native_workspace, owner_sessionmaker):  # noqa: F811
    import hashlib
    from sqlalchemy import select
    from app.models.workspace_resource import WorkspaceResourceManifest
    from tests.integration.test_m1g28_runtime_source_reads_postgres import _create, _read

    n = native_workspace
    path = "workspace/member-edited.md"
    target = await _create(n, path, "Original report")
    target.write_text("Member's current report")
    async with owner_sessionmaker() as db:
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == path))
        manifest.authority_state = "quarantined"
        await db.commit()
    assert "Member's current report" in await _read(n, "read_file", path)
    response = await n.client.put(f"/agents/{n.agent}/files/content", params={"path": path},
        json={"content": "Member's next revision", "expected_current_hash": hashlib.sha256(target.read_bytes()).hexdigest()})
    assert response.status_code == 200, response.text
    assert "Member's next revision" in await _read(n, "read_file", path)
    assert "PRIVATE_FOREIGN_FILE_BODY" not in await _read(n, "read_file", n.path)
