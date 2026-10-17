"""Packaged scripts retain native source authority and deliver owned artifacts."""

import hashlib
import json
import platform
import shlex

import pytest
from sqlalchemy import select

from app.models.capability_policy import CapabilityPolicy
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g40_session_file_actor_postgres import _files

pytestmark = [pytest.mark.usefixtures("migrated_pg_url"), pytest.mark.skipif(platform.system() != "Darwin",
    reason="Native macOS process gate; Linux delivery is deferred.")]


async def _skill(n, db_factory, program, suffix="py", *, base=None):
    scripts = (base or n.root) / "skills/source-probe/scripts"
    scripts.mkdir(parents=True)
    (scripts.parent / "SKILL.md").write_text(
        "---\nname: Source Probe\ndescription: Controlled source probe.\n---\nRun scripts/probe." + suffix + ".\n")
    target = scripts / ("probe." + suffix)
    target.write_text(program)
    async with db_factory() as db:
        for capability in ("agent.skill.read", "agent.skill.execute", "workspace.file.read"):
            if await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == n.tenant,
                CapabilityPolicy.capability == capability)):
                continue
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        await db.commit()
    return target


async def _tool(n, name, args, **kwargs):
    return await n.service.execute(name, args, agent_id=n.agent, user_id=n.user, session_id=str(n.session),
        runtime_task_id=str(n.run), emit_runtime_hooks=False, **kwargs)


@pytest.mark.parametrize("suffix", ["py", "sh", "js"])
async def test_skill_process_keeps_source_boundary_and_returns_readable_owned_output(
    native_workspace, owner_sessionmaker, suffix,  # noqa: F811
):
    n = native_workspace
    _own, foreign, paths = await _files(n, owner_sessionmaker, "t0")
    private = n.root / paths[foreign]
    original = private.read_bytes()
    response = await n.client.put(f"/agents/{n.agent}/files/content", params={"path": "workspace/allowed.txt"},
        json={"content": "AUTHORIZED_SKILL_INPUT"})
    assert response.status_code == 200, response.text
    program = ("from pathlib import Path\nprint(Path('allowed.txt').read_text())\n"
        "Path('skill-result.txt').write_text('AUTHORIZED_SKILL_DELIVERY')\nprint('SKILL_OK')\n")
    for path, denied in [("private-report.md", "WORKSPACE_DENIED"), (str(private), "SOURCE_DENIED")]:
        program += f"try:\n print(Path({path!r}).read_text())\nexcept OSError:\n print({denied!r})\n"
    if suffix == "sh":
        program = "cat allowed.txt\nprintf AUTHORIZED_SKILL_DELIVERY > skill-result.txt\nprintf 'SKILL_OK\\n'\n"
        for path, denied in [("private-report.md", "WORKSPACE_DENIED"), (str(private), "SOURCE_DENIED")]:
            program += f"if cat {shlex.quote(path)}; then :; else printf '{denied}\\n'; fi\n"
    elif suffix == "js":
        program = ("const fs = require('fs'); console.log(fs.readFileSync('allowed.txt','utf8'));\n"
            "fs.writeFileSync('skill-result.txt','AUTHORIZED_SKILL_DELIVERY'); console.log('SKILL_OK');\n")
        for path, denied in [("private-report.md", "WORKSPACE_DENIED"), (str(private), "SOURCE_DENIED")]:
            program += (f"try {{console.log(fs.readFileSync({json.dumps(path)},'utf8'));}} "
                f"catch(e) {{if(!['EACCES','EPERM','ENOENT'].includes(e.code)) throw e; console.log('{denied}');}}\n")
    await _skill(n, owner_sessionmaker, program, suffix)
    assert "Run scripts/probe." + suffix in str(await _tool(n, "load_skill", {"name": "source-probe"}))
    raw = await _tool(n, "run_skill_tool", {"skill": "source-probe", "script": "scripts/probe." + suffix})
    result = str(raw)
    assert "AUTHORIZED_SKILL_INPUT" in result and "SKILL_OK" in result, result
    assert "PRIVATE_FOREIGN_FILE_BODY" not in result and "WORKSPACE_DENIED" in result, result
    assert "OTHER_REQUESTER_PRIVATE_EVIDENCE" not in result and "SOURCE_DENIED" in result, result
    assert raw.metadata["code_execution_evidence"]["isolation"] == "sandbox-exec", raw.metadata
    assert private.read_bytes() == original and n.target.read_text() == "PRIVATE_FOREIGN_FILE_BODY"
    assert (n.root / "workspace/skill-result.txt").read_text() == "AUTHORIZED_SKILL_DELIVERY"
    assert "AUTHORIZED_SKILL_DELIVERY" in str(await _tool(n, "read_file", {"path": "workspace/skill-result.txt"}))


async def test_skill_output_is_registered_for_the_actual_requester(native_workspace, owner_sessionmaker):  # noqa: F811
    n = native_workspace
    await _skill(n, owner_sessionmaker, "from pathlib import Path\nPath('skill-result.txt').write_text('OWNED_SKILL_OUTPUT')\n")
    trace = {}
    raw = await _tool(n, "run_skill_tool", {"skill": "source-probe", "script": "scripts/probe.py"}, trace_metadata_sink=trace)
    assert (n.root / "workspace/skill-result.txt").read_text() == "OWNED_SKILL_OUTPUT", raw
    assert "OWNED_SKILL_OUTPUT" in str(await _tool(n, "read_file", {"path": "workspace/skill-result.txt"}))
    assert any(item["path"] == "workspace/skill-result.txt" for item in raw.artifacts)
    assert trace["workspace_mutation_evidence_captured"] is True
    assert trace["workspace_mutation_states"]["workspace/skill-result.txt"]["sha256"] == hashlib.sha256(b"OWNED_SKILL_OUTPUT").hexdigest()


async def test_skill_cannot_enable_unsandboxed_fallback(native_workspace, owner_sessionmaker, monkeypatch):  # noqa: F811
    n = native_workspace
    await _skill(n, owner_sessionmaker, "from pathlib import Path\nPath('bypass.txt').write_text('UNAUTHORIZED_EFFECT')\n")
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "off")
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "1")
    raw = await _tool(n, "run_skill_tool", {"skill": "source-probe", "script": "scripts/probe.py"})
    assert raw.metadata["code_execution_evidence"]["isolation"] == "unavailable", raw
    assert not (n.root / "workspace/bypass.txt").exists()


async def test_read_grant_does_not_allow_skill_to_overwrite_a_foreign_file(native_workspace, owner_sessionmaker):  # noqa: F811
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission

    n = native_workspace
    await _skill(n, owner_sessionmaker, "from pathlib import Path\n"
        "print(Path('private-report.md').read_text())\nPath('private-report.md').write_text('UNAUTHORIZED_OVERWRITE')\n")
    async with owner_sessionmaker() as db:
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, n.path), actions=["read"], conditions={}))
        await db.commit()
    raw = await _tool(n, "run_skill_tool", {"skill": "source-probe", "script": "scripts/probe.py"})
    assert "workspace_resource_collision" in str(raw), raw
    assert n.target.read_text() == "PRIVATE_FOREIGN_FILE_BODY"


@pytest.mark.parametrize("source", ["hardlink", "private_package"])
async def test_skill_source_itself_needs_current_file_authority(native_workspace, owner_sessionmaker, source):  # noqa: F811
    n = native_workspace
    base = n.root / "workspace/private" if source == "private_package" else n.root
    script = await _skill(n, owner_sessionmaker, "print('UNAUTHORIZED_SKILL_SOURCE_EXECUTED')\n", base=base)
    if source == "hardlink":
        alias = n.root / "workspace/private-source.py"
        alias.hardlink_to(script)
    raw = await _tool(n, "run_skill_tool", {"skill": "source-probe", "script": "scripts/probe.py"})
    assert "UNAUTHORIZED_SKILL_SOURCE_EXECUTED" not in str(raw), raw
    assert "auth_or_permission" in str(raw), raw


async def test_private_skill_source_explicit_read_grant_is_usable_and_revocable(native_workspace, owner_sessionmaker):  # noqa: F811
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.services.workspace_resource_authority import register_workspace_path

    n = native_workspace
    script = await _skill(n, owner_sessionmaker, "print('EXPLICITLY_GRANTED_SKILL_RAN')\n", base=n.root / "workspace/private")
    path = script.relative_to(n.root).as_posix()
    async with owner_sessionmaker() as db:
        await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
            owner_user_id=n.foreign, root_session_id=None, source="fixture")
        grant = ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, path), actions=["read"], conditions={})
        db.add(grant)
        await db.commit()
        grant_id = grant.id
    args = {"skill": "source-probe", "script": "scripts/probe.py"}
    raw = await _tool(n, "run_skill_tool", args)
    assert "EXPLICITLY_GRANTED_SKILL_RAN" in str(raw), raw
    assert raw.metadata["code_execution_evidence"]["isolation"] == "sandbox-exec", raw.metadata
    async with owner_sessionmaker() as db:
        (await db.get(ResourcePermission, grant_id)).actions = []
        await db.commit()
    denied = await _tool(n, "run_skill_tool", args)
    assert "EXPLICITLY_GRANTED_SKILL_RAN" not in str(denied) and "auth_or_permission" in str(denied), denied
