"""Exercise network policy at the real local OS-sandbox boundary."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from app.runtime.ccplus_contracts import SandboxProfile
from app.services.code_execution import local_provider
from app.services.code_execution.service import execute_agent_command
from app.services.subprocess_sandbox import AgentSandboxCommand, SandboxBuildSpec, probe_os_sandbox_capability

_CAPABILITY = probe_os_sandbox_capability()


@asynccontextmanager
async def _loopback_server():
    received = []
    async def connection(reader, writer):
        try:
            received.append((await asyncio.wait_for(reader.readline(), 3)).decode().strip())
            writer.write(b"LOOPBACK_RECEIPT\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(connection, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"HOST_LIVENESS\n")
        await writer.drain()
        assert await reader.readline() == b"LOOPBACK_RECEIPT\n"
        writer.close()
        await writer.wait_closed()
        yield port, received
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.skipif(not _CAPABILITY.available, reason=_CAPABILITY.reason)
@pytest.mark.parametrize("policy,spec_access,configured,allowed", [
    ("allow-all", None, None, True),
    ("deny", True, None, False),
    ("allow-all", False, None, False),
    (None, True, None, True),
    (None, None, "allow-all", True),
    (None, True, "deny-all", False),
])
async def test_local_policy_controls_actual_tcp_without_exposing_host_secrets(
    tmp_path, monkeypatch, policy, spec_access, configured, allowed,
):
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "0")
    monkeypatch.delenv("HIVE_CODE_EXEC_NETWORK_POLICY", raising=False)
    if configured is not None:
        monkeypatch.setenv("HIVE_CODE_EXEC_NETWORK_POLICY", configured)
    work, home = tmp_path / "work", tmp_path / "home"
    work.mkdir()
    home.mkdir()
    async with _loopback_server() as (port, received):
        script = f'''import json,os,socket
try:
 with socket.create_connection(("127.0.0.1", {port}), timeout=2) as client:
  client.sendall(b"SANDBOX_REQUEST\\n")
  outcome = client.recv(128).decode().strip()
except OSError:
 outcome = "NETWORK_BLOCKED"
print(json.dumps({{"outcome":outcome,"secret_present":"OPENAI_API_KEY" in os.environ}}))
'''
        result = await execute_agent_command([str(Path(sys.executable).resolve()), "-I", "-c", script], work_dir=work,
            env={"HOME": str(home), "OPENAI_API_KEY": "synthetic-native-secret-canary"}, timeout=5,
            network_policy=policy, sandbox_spec=SandboxBuildSpec(profile=SandboxProfile.WORKSPACE_WRITE,
                network_access=spec_access) if spec_access is not None else None)
        assert result.error is None and result.exit_code == 0, result
        observed = json.loads(result.stdout)
        (tmp_path / "network-observation.json").write_text(json.dumps({"result": observed, "evidence": result.evidence,
            "received": received, "policy": policy, "configured": configured, "spec_access": spec_access}, indent=2))
        assert observed == {"outcome": "LOOPBACK_RECEIPT" if allowed else "NETWORK_BLOCKED", "secret_present": False}
        assert ("SANDBOX_REQUEST" in received) is allowed
        assert result.evidence["network_policy"] == ("allow" if allowed else "deny-all")
        assert result.evidence["isolation"] in {"sandbox-exec", "bubblewrap"}


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["allowlist:registry.npmjs.org", "unsupported-policy"])
async def test_local_unsupported_policy_does_not_start_the_command(tmp_path, monkeypatch, policy):
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")
    result = await execute_agent_command([str(Path(sys.executable).resolve()), "-I", "-c", "open('effect', 'w').write('must not run')"],
        work_dir=tmp_path, env={"HOME": str(tmp_path)}, timeout=5, network_policy=policy)
    assert result.error and result.evidence["status"] == "unsupported"
    assert result.evidence["reason_code"] == "network_policy_unsupported"
    assert not (tmp_path / "effect").exists()


@pytest.mark.asyncio
async def test_explicit_network_denial_cannot_use_raw_development_bypass(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")
    monkeypatch.setattr(local_provider, "build_sandboxed_agent_command", lambda command, **_kwargs: AgentSandboxCommand(command=command))
    result = await execute_agent_command([str(Path(sys.executable).resolve()), "-I", "-c", "open('effect', 'w').write('must not run')"],
        work_dir=tmp_path, env={"HOME": str(tmp_path)}, timeout=5, network_policy="deny-all")
    assert result.error and result.evidence["status"] == "unsupported"
    assert result.evidence["reason_code"] == "network_isolation_unavailable"
    assert not (tmp_path / "effect").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(not _CAPABILITY.available, reason=_CAPABILITY.reason)
async def test_real_governance_hook_can_allow_work_after_enforced_network_denial(tmp_path, monkeypatch):
    from app.tools.governance_resolver import run_sandboxed_governance_hook
    from app.tools.hook_governance import GovernanceHookSpec

    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")
    monkeypatch.setenv("HIVE_CODE_EXEC_NETWORK_POLICY", "allow-all")
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "0")
    async def workspace(*_args):
        return tmp_path
    monkeypatch.setattr("app.tools.workspace.ensure_workspace", workspace)
    async with _loopback_server() as (port, received):
        script = tmp_path / "hook.py"
        script.write_text(f'''import json,socket,sys
payload = json.load(sys.stdin)
assert payload["tool_name"] == "run_command"
try:
 socket.create_connection(("127.0.0.1", {port}), timeout=2).close()
 reason = "NETWORK_UNEXPECTEDLY_OPEN"
except OSError:
 reason = "NETWORK_BLOCKED"
print(json.dumps({{"decision":"allow" if reason == "NETWORK_BLOCKED" else "deny","reason":reason}}))
''')
        spec = GovernanceHookSpec(key="native-network-gate", layer="company", kind="command", matcher="*", decision=None,
            reason="", arg_rules=(), command=f"{shlex.quote(str(Path(sys.executable).resolve()))} -I {shlex.quote(str(script))}",
            timeout_seconds=5, enabled=True)
        verdict = await run_sandboxed_governance_hook(spec, {"tool_name": "run_command", "agent_id": str(uuid4()), "tenant_id": str(uuid4())})
        (tmp_path / "hook-observation.json").write_text(json.dumps({"decision":verdict.decision,"reason":verdict.reason,"received":received}))
        assert verdict.decision == "allow" and verdict.reason == "NETWORK_BLOCKED", verdict
        assert received == ["HOST_LIVENESS"]
        assert not list((tmp_path / ".hive/hook_payloads").glob("*.json"))
