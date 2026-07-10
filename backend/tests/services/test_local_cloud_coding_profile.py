"""CCPlus V1 reconciliation §7 — "local/cloud coding" provider matrix.

Reconciliation §7 row "local/cloud coding" requires that the agent
code-execution provider matrix exists and honors the CLAUDE.md *Code Execution
Provider Invariant*:

  - trusted/local hosts use the local OS sandbox (``local_os_sandbox``),
  - Railway/cloud uses ``vercel_sandbox``,
  - provider selection is gated by ``HIVE_CODE_EXEC_PROVIDER`` (env/config),
  - a raw host subprocess is **never** a silent fallback.

``app/services/code_execution/service.py`` is the selection seam
(``configured_code_execution_provider`` + ``execute_agent_command``). These
tests drive the real selector with monkeypatched providers and assert the real
invariant: the right provider is dispatched per env, an unknown provider returns
an error result (no raw subprocess), and the local provider's only unsandboxed
path is the explicit ``HIVE_ALLOW_UNSANDBOXED_CODE_EXEC`` bypass — never a
silent fallback.

Function names contain ``local_cloud`` / ``coding_profile`` so ``pytest -k
local_cloud`` or ``-k coding_profile`` collects them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.code_execution import service as code_exec_service
from app.services.code_execution.contracts import CodeExecutionResult


@pytest.fixture
def code_exec_calls(monkeypatch):
    """Replace both real providers with recorders so we can assert dispatch.

    Returns a dict with ``local`` / ``vercel`` lists; each provider records the
    kwargs it was called with and returns a tagged CodeExecutionResult.
    """
    calls: dict[str, list[dict]] = {"local": [], "vercel": []}

    async def fake_local(command, *, work_dir, env, timeout, spec=None):
        calls["local"].append({"command": command, "work_dir": work_dir, "env": env, "timeout": timeout, "spec": spec})
        return CodeExecutionResult(stdout="local-ok", evidence={"provider": "local_os_sandbox"})

    async def fake_vercel(command, *, work_dir, env, timeout, runtime=None, network_policy=None):
        calls["vercel"].append(
            {
                "command": command,
                "work_dir": work_dir,
                "env": env,
                "timeout": timeout,
                "runtime": runtime,
                "network_policy": network_policy,
            }
        )
        return CodeExecutionResult(stdout="vercel-ok", evidence={"provider": "vercel_sandbox"})

    monkeypatch.setattr(code_exec_service, "execute_local_sandboxed_command", fake_local)
    monkeypatch.setattr(code_exec_service, "execute_vercel_sandbox_command", fake_vercel)
    return calls


def test_local_cloud_provider_defaults_to_local_os_sandbox(monkeypatch) -> None:
    """With no env set, the configured provider is the trusted local OS sandbox."""
    monkeypatch.delenv("HIVE_CODE_EXEC_PROVIDER", raising=False)
    assert code_exec_service.configured_code_execution_provider() == "local_os_sandbox"


def test_local_cloud_provider_is_gated_by_env(monkeypatch) -> None:
    """The cloud provider is selected only when HIVE_CODE_EXEC_PROVIDER asks for it."""
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "vercel_sandbox")
    assert code_exec_service.configured_code_execution_provider() == "vercel_sandbox"

    # Case / whitespace normalised so config drift doesn't silently fall through.
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "  Vercel_Sandbox  ")
    assert code_exec_service.configured_code_execution_provider() == "vercel_sandbox"


@pytest.mark.asyncio
async def test_local_cloud_coding_profile_routes_local_for_trusted_host(
    monkeypatch, tmp_path: Path, code_exec_calls
) -> None:
    """Trusted/local profile dispatches to the local OS sandbox provider."""
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")

    result = await code_exec_service.execute_agent_command(
        ["python3", "-c", "print(1)"],
        work_dir=tmp_path,
        env={"HOME": str(tmp_path)},
        timeout=5,
    )

    assert result.stdout == "local-ok"
    assert result.evidence["provider"] == "local_os_sandbox"
    assert len(code_exec_calls["local"]) == 1
    assert code_exec_calls["vercel"] == []


@pytest.mark.asyncio
async def test_local_cloud_coding_profile_routes_vercel_for_cloud(monkeypatch, tmp_path: Path, code_exec_calls) -> None:
    """Cloud profile dispatches to the Vercel sandbox provider and forwards runtime/network."""
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "vercel_sandbox")

    result = await code_exec_service.execute_agent_command(
        ["python3", "-c", "print(1)"],
        work_dir=tmp_path,
        env={"HOME": str(tmp_path)},
        timeout=5,
        runtime="python3.13",
        network_policy="deny-all",
    )

    assert result.stdout == "vercel-ok"
    assert result.evidence["provider"] == "vercel_sandbox"
    assert len(code_exec_calls["vercel"]) == 1
    assert code_exec_calls["local"] == []
    assert code_exec_calls["vercel"][0]["runtime"] == "python3.13"
    assert code_exec_calls["vercel"][0]["network_policy"] == "deny-all"


@pytest.mark.asyncio
async def test_local_cloud_coding_profile_no_raw_subprocess_fallback_for_unknown_provider(
    monkeypatch, tmp_path: Path, code_exec_calls
) -> None:
    """An unknown provider returns an error result — NOT a raw host subprocess.

    This is the heart of the Code Execution Provider Invariant: if neither the
    local sandbox nor the cloud sandbox matches, execution must be refused with
    an actionable error, never silently downgraded to an ungoverned subprocess.
    """
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "raw_subprocess")

    result = await code_exec_service.execute_agent_command(
        ["python3", "-c", "print(1)"],
        work_dir=tmp_path,
        env={"HOME": str(tmp_path)},
        timeout=5,
    )

    assert result.error is not None
    assert "provider unavailable" in result.error.lower()
    assert "raw_subprocess" in result.error
    # Critically: neither sandbox provider ran, and no command executed.
    assert code_exec_calls["local"] == []
    assert code_exec_calls["vercel"] == []


def test_local_cloud_service_source_never_spawns_raw_subprocess() -> None:
    """The selector module itself must not import/spawn raw subprocesses.

    The local provider owns the (sandbox-wrapped) subprocess call; the selector
    in service.py only dispatches. If someone added a ``subprocess`` /
    ``asyncio.create_subprocess`` fallback into the selector to "make it work",
    this guard fails.
    """
    service_src = Path(code_exec_service.__file__).read_text(encoding="utf-8")
    assert "import subprocess" not in service_src
    assert "create_subprocess" not in service_src
    assert "os.system" not in service_src


@pytest.mark.asyncio
async def test_local_cloud_coding_profile_local_sandbox_refuses_unsandboxed_without_flag(
    monkeypatch, tmp_path: Path
) -> None:
    """The local provider refuses to run unsandboxed unless the explicit bypass flag is set.

    This exercises the *real* local provider (no monkeypatch of the provider):
    with the OS sandbox forced unavailable (``HIVE_CODE_SANDBOX_MODE=none``) and
    no ``HIVE_ALLOW_UNSANDBOXED_CODE_EXEC`` flag, execution must error out rather
    than fall through to a bare subprocess. With the explicit flag set, the
    bypass is allowed — proving the only unsandboxed path is the documented,
    opt-in development bypass, never a silent fallback.
    """
    from app.services.code_execution.local_provider import execute_local_sandboxed_command

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "none")
    monkeypatch.delenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", raising=False)

    blocked = await execute_local_sandboxed_command(
        ["python3", "-c", "print('should-not-run')"],
        work_dir=tmp_path,
        env={"HOME": str(home)},
        timeout=5,
    )
    assert blocked.error is not None
    assert "sandbox unavailable" in blocked.error.lower()
    assert blocked.stdout == ""

    # Explicit opt-in bypass is the ONLY way to run unsandboxed.
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "1")
    allowed = await execute_local_sandboxed_command(
        ["python3", "-c", "print('hive-local-cloud-bypass-ok')"],
        work_dir=tmp_path,
        env={"HOME": str(home)},
        timeout=15,
    )
    assert allowed.error is None
    assert "hive-local-cloud-bypass-ok" in allowed.stdout
    assert allowed.evidence["isolation"] == "unsandboxed_dev_bypass"
