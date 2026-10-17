"""Copied process inputs retain the live source and exact-byte receipt checks."""

import os
import platform
import shlex
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.capability_policy import CapabilityPolicy
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g28_runtime_source_reads_postgres import _create
from tests.integration.test_m1g33_workspace_source_audiences_postgres import workspace_source  # noqa: F401
from tests.integration.test_m1g44_skill_source_authority_postgres import _skill

pytestmark = [pytest.mark.usefixtures("migrated_pg_url"), pytest.mark.skipif(platform.system() != "Darwin",
    reason="Native macOS process gate; Linux delivery is deferred.")]


@pytest.mark.parametrize("tool", ["execute_code", "run_command", "run_skill_tool"])
@pytest.mark.parametrize("change", ["revoked", "tampered", "copied_bytes"])
async def test_process_materialization_keeps_source_receipts_without_blocking_other_inputs(
    workspace_source, owner_sessionmaker, monkeypatch, tool, change,  # noqa: F811
):
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.agent_tool_domains import code_exec

    s, n = workspace_source, workspace_source.n
    await _create(n, "workspace/allowed.txt", "AUTHORIZED_PROCESS_INPUT")
    program = ("from pathlib import Path\nprint(Path('allowed.txt').read_text())\n"
        "Path('delivery.txt').write_text('AUTHORIZED_PROCESS_DELIVERY')\n"
        "try:\n print(Path('derived.md').read_text())\nexcept OSError:\n print('DERIVED_INPUT_DENIED')\n")
    await _skill(n, owner_sessionmaker, program)
    grant_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        for capability in ("workspace.code.execute", "workspace.command.execute"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        (await db.get(User, n.user)).role = "org_admin"
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == s.source))
        manifest.owner_user_id, manifest.root_session_id = n.foreign, None
        db.add(ResourcePermission(id=grant_id, tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, s.source), actions=["read"], conditions={}))
        await db.commit()
    args = {"code": program, "language": "python"} if tool == "execute_code" else {
        "command": "python3 -c " + shlex.quote(program)} if tool == "run_command" else {
        "skill": "source-probe", "script": "scripts/probe.py"}

    async def call():
        return await n.service.execute(tool, args, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False)

    positive = await call()
    assert "AUTHORIZED_PROCESS_INPUT" in str(positive) and s.body in str(positive), positive
    assert positive.metadata["code_execution_evidence"]["isolation"] == "sandbox-exec", positive.metadata
    target = n.root / s.derived
    original, stamp = target.read_bytes(), target.stat()

    def tamper():
        target.write_text("FORGED_EXECUTION_INPUT".ljust(len(original)))
        os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    if change == "revoked":
        async with owner_sessionmaker() as db:
            (await db.get(ResourcePermission, grant_id)).revoked_at = datetime.now(timezone.utc)
            await db.commit()
        browser = await n.client.get(f"/agents/{n.agent}/files/content", params={"path": s.derived})
        assert browser.status_code == 200 and s.body in browser.text, browser.text
    elif change == "tampered":
        tamper()
    else:
        copy_resource = code_exec._copy_workspace_resource

        def change_at_copy(source, destination, path, **kwargs):
            if source == n.root and path == s.derived:
                tamper()
            return copy_resource(source, destination, path, **kwargs)

        monkeypatch.setattr(code_exec, "_copy_workspace_resource", change_at_copy)
    negative = await call()
    assert "AUTHORIZED_PROCESS_INPUT" in str(negative) and "DERIVED_INPUT_DENIED" in str(negative), negative
    assert s.body not in str(negative) and "FORGED_EXECUTION_INPUT" not in str(negative), negative
    denied = negative.metadata["workspace_input_denials"]
    assert any(row["path"] == s.derived and row["reason_code"] == (
        "content_source_access_denied" if change == "revoked" else "workspace_content_unverified") for row in denied), denied
    assert "workspace_input_denials" in str(negative)
    assert (n.root / "workspace/delivery.txt").read_text() == "AUTHORIZED_PROCESS_DELIVERY"
    if change == "revoked":
        async with owner_sessionmaker() as db:
            (await db.get(ResourcePermission, grant_id)).revoked_at = None
            await db.commit()
        recovered = await call()
        assert s.body in str(recovered) and "AUTHORIZED_PROCESS_INPUT" in str(recovered), recovered
    else:
        async with owner_sessionmaker() as db:
            manifest = await db.scalar(select(WorkspaceResourceManifest).where(
                WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == s.derived))
            assert manifest.authority_state == "quarantined"
        assert target.read_text().strip() == "FORGED_EXECUTION_INPUT"


async def test_selected_receipted_skill_script_cannot_execute_rewritten_bytes(native_workspace, owner_sessionmaker):  # noqa: F811
    from app.models.workspace_resource import WorkspaceResourceManifest

    n = native_workspace
    script = await _skill(n, owner_sessionmaker, "print('RECEIPTED_SKILL_EXECUTED')\n", base=n.root / "workspace/private")
    path = script.relative_to(n.root).as_posix()
    script.unlink()
    await _create(n, path, "print('RECEIPTED_SKILL_EXECUTED')\n")

    async def call():
        return await n.service.execute("run_skill_tool", {"skill": "source-probe", "script": "scripts/probe.py"},
            agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False)

    positive = await call()
    assert "RECEIPTED_SKILL_EXECUTED" in str(positive), positive
    assert positive.metadata["code_execution_evidence"]["isolation"] == "sandbox-exec", positive.metadata
    script.write_text("print('FORGED_SKILL_EXECUTED')\n")
    negative = await call()
    assert "FORGED_SKILL_EXECUTED" not in str(negative), negative
    assert "workspace_content_unverified" in str(negative), negative
    assert "code_execution_evidence" not in negative.metadata
    async with owner_sessionmaker() as db:
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == path))
        assert manifest.authority_state == "quarantined"
    assert script.read_text() == "print('FORGED_SKILL_EXECUTED')\n"
