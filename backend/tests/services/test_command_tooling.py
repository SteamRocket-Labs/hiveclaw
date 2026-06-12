from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_run_command_executes_inside_workspace(tmp_path: Path):
    from app.services.agent_tool_domains.code_exec import _run_command

    workspace_root = tmp_path / str(uuid4())
    result = await _run_command(
        workspace_root,
        {
            "command": "pwd",
            "timeout": 5,
        },
    )

    assert str((workspace_root / "workspace").resolve()) in result


@pytest.mark.asyncio
async def test_run_command_fails_closed_without_sandbox_or_explicit_dev_bypass(tmp_path: Path, monkeypatch):
    from app.services.agent_tool_domains.code_exec import _run_command

    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "none")
    monkeypatch.delenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", raising=False)

    result = await _run_command(
        tmp_path,
        {
            "command": "pwd",
            "timeout": 5,
        },
    )

    assert "sandbox unavailable" in result.lower()
    assert "HIVE_ALLOW_UNSANDBOXED_CODE_EXEC=1" in result


@pytest.mark.asyncio
async def test_execute_code_fails_closed_without_sandbox_or_explicit_dev_bypass(tmp_path: Path, monkeypatch):
    from app.services.agent_tool_domains.code_exec import _execute_code

    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "none")
    monkeypatch.delenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", raising=False)

    result = await _execute_code(
        tmp_path,
        {
            "language": "python",
            "code": "print('hello')",
            "timeout": 5,
        },
    )

    assert "sandbox unavailable" in result.lower()
    assert "HIVE_ALLOW_UNSANDBOXED_CODE_EXEC=1" in result


@pytest.mark.asyncio
async def test_run_command_blocks_high_risk_commands(tmp_path: Path):
    from app.services.agent_tool_domains.code_exec import _run_command

    result = await _run_command(
        tmp_path,
        {
            "command": "docker ps",
            "timeout": 5,
        },
    )

    assert "Blocked" in result


def test_execution_environment_does_not_inherit_platform_secrets(tmp_path: Path, monkeypatch):
    from app.services.agent_tool_domains.code_exec import _prepare_execution_environment

    monkeypatch.setenv("JWT_SECRET_KEY", "jwt-secret")
    monkeypatch.setenv("SECRETS_MASTER_KEY", "master-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    work_dir, env = _prepare_execution_environment(tmp_path)

    assert work_dir == (tmp_path / "workspace").resolve()
    assert env["HOME"].startswith("/tmp/exec_home_")
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "en_US.UTF-8"
    assert "JWT_SECRET_KEY" not in env
    assert "SECRETS_MASTER_KEY" not in env
    assert "DATABASE_URL" not in env
    assert "OPENAI_API_KEY" not in env
