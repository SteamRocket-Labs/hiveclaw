"""Real native OS processes must retain the copied workspace's source boundary."""

import json
import platform
import shlex

import pytest

from app.models.capability_policy import CapabilityPolicy
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g40_session_file_actor_postgres import _files

pytestmark = [pytest.mark.usefixtures("migrated_pg_url"), pytest.mark.skipif(platform.system() != "Darwin",
    reason="Native macOS process gate; Linux delivery is deferred.")]


@pytest.mark.parametrize("tool,language", [("execute_code", "python"), ("execute_code", "bash"),
    ("execute_code", "node"), ("run_command", "python")])
async def test_real_native_process_reads_only_authorized_source_files_and_delivers(
    native_workspace, owner_sessionmaker, tool, language,  # noqa: F811
):
    assert platform.system() == "Darwin", "This delivery requires its actual macOS OS-process gate; Linux is deferred."
    n = native_workspace
    _own, foreign, paths = await _files(n, owner_sessionmaker, "t0")
    private = n.root / paths[foreign]
    original = private.read_bytes()
    other_job = n.root.parent.parent / "other-job-private.txt"
    other_job.write_text("OTHER_JOB_PRIVATE_BODY")
    response = await n.client.put(f"/agents/{n.agent}/files/content", params={"path": "workspace/allowed.txt"},
        json={"content": "AUTHORIZED_PROCESS_INPUT"})
    assert response.status_code == 200, response.text
    async with owner_sessionmaker() as db:
        for capability in ("workspace.code.execute", "workspace.command.execute"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        await db.commit()
    program = ("from pathlib import Path\nimport tempfile\n"
        "print(Path('allowed.txt').read_text())\n"
        "Path('delivered.txt').write_text('AUTHORIZED_PROCESS_DELIVERY')\n"
        "with tempfile.TemporaryDirectory() as d:\n Path(d, 'scratch.txt').write_text('scratch')\n"
        "print('PROCESS_OK')\ntry:\n"
        f" print(Path({str(private)!r}).read_text())\nexcept OSError:\n print('SOURCE_DENIED')\n"
        f"try:\n print(Path({str(other_job)!r}).read_text())\nexcept OSError:\n print('JOB_SOURCE_DENIED')\n"
        f"try:\n Path({str(private)!r}).write_text('UNAUTHORIZED_OVERWRITE')\nexcept OSError:\n print('SOURCE_WRITE_DENIED')\n")
    if language == "node":
        program = ("const fs = require('fs'); const os = require('os');\n"
            "console.log(fs.readFileSync('allowed.txt', 'utf8'));\n"
            "fs.writeFileSync('delivered.txt', 'AUTHORIZED_PROCESS_DELIVERY');\n"
            "const d=fs.mkdtempSync(os.tmpdir()+'/native-'); fs.writeFileSync(d+'/scratch.txt','scratch');\n"
            "console.log('PROCESS_OK');\n"
            f"try {{ console.log(fs.readFileSync({json.dumps(str(private))},'utf8')); }} "
            "catch(e) { if(e.code!=='EACCES' && e.code!=='EPERM' && e.code!=='ENOENT') throw e; console.log('SOURCE_DENIED'); }\n"
            f"try {{ console.log(fs.readFileSync({json.dumps(str(other_job))},'utf8')); }} "
            "catch(e) { if(e.code!=='EACCES' && e.code!=='EPERM' && e.code!=='ENOENT') throw e; console.log('JOB_SOURCE_DENIED'); }\n"
            f"try {{ fs.writeFileSync({json.dumps(str(private))},'UNAUTHORIZED_OVERWRITE'); }} "
            "catch(e) { if(e.code!=='EACCES' && e.code!=='EPERM' && e.code!=='ENOENT') throw e; console.log('SOURCE_WRITE_DENIED'); }\n")
    elif language == "bash":
        program = ("cat allowed.txt\nprintf AUTHORIZED_PROCESS_DELIVERY > delivered.txt\n"
            "scratch=$(mktemp -d); printf scratch > \"$scratch/scratch.txt\"\nprintf 'PROCESS_OK\\n'\n"
            f"if cat {shlex.quote(str(private))}; then :; else printf 'SOURCE_DENIED\\n'; fi\n"
            f"if cat {shlex.quote(str(other_job))}; then :; else printf 'JOB_SOURCE_DENIED\\n'; fi\n"
            f"if printf UNAUTHORIZED_OVERWRITE > {shlex.quote(str(private))}; then :; else printf 'SOURCE_WRITE_DENIED\\n'; fi\n")
    arguments = {"language": language, "code": program, "timeout": 20} if tool == "execute_code" else {
        "command": "python3 -c " + shlex.quote(program), "timeout": 20}
    raw = await n.service.execute(tool, arguments, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False)
    result = str(raw)
    assert "AUTHORIZED_PROCESS_INPUT" in result and "PROCESS_OK" in result, result
    assert "OTHER_REQUESTER_PRIVATE_EVIDENCE" not in result and "SOURCE_DENIED" in result, result
    assert "OTHER_JOB_PRIVATE_BODY" not in result and "JOB_SOURCE_DENIED" in result, result
    assert "SOURCE_WRITE_DENIED" in result, result
    assert raw.metadata["code_execution_evidence"]["isolation"] == "sandbox-exec", raw.metadata
    assert (n.root / "workspace/delivered.txt").read_text() == "AUTHORIZED_PROCESS_DELIVERY"
    assert private.read_bytes() == original
    assert other_job.read_text() == "OTHER_JOB_PRIVATE_BODY"


@pytest.mark.parametrize("tool", ["execute_code", "run_command"])
async def test_native_protected_sources_do_not_fall_back_to_unsandboxed_development(
    native_workspace, owner_sessionmaker, monkeypatch, tool,  # noqa: F811
):
    n = native_workspace
    async with owner_sessionmaker() as db:
        for capability in ("workspace.code.execute", "workspace.command.execute"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        await db.commit()
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "off")
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "1")
    program = "from pathlib import Path; Path('bypass.txt').write_text('UNAUTHORIZED_EFFECT')"
    arguments = {"language": "python", "code": program} if tool == "execute_code" else {
        "command": "python3 -c " + shlex.quote(program)}
    raw = await n.service.execute(tool, arguments, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False)
    assert raw.metadata["code_execution_evidence"]["isolation"] == "unavailable", raw.metadata
    assert "Protected session sources" in str(raw)
    assert not (n.root / "workspace/bypass.txt").exists()
