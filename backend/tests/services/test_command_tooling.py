from __future__ import annotations

import os
import sys
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


def test_execution_environment_strips_registry_url_credentials(tmp_path: Path, monkeypatch):
    from app.services.agent_tool_domains.code_exec import _prepare_execution_environment

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:token@example.com/simple")
    monkeypatch.setenv(
        "PIP_EXTRA_INDEX_URL",
        "https://alpha:secret@extra.example/simple https://public.example/simple",
    )
    monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://npm-user:npm-token@registry.example/npm/")

    _work_dir, env = _prepare_execution_environment(tmp_path)

    assert env["PIP_INDEX_URL"] == "https://example.com/simple"
    assert env["PIP_EXTRA_INDEX_URL"] == "https://extra.example/simple https://public.example/simple"
    assert env["NPM_CONFIG_REGISTRY"] == "https://registry.example/npm/"
    assert "token" not in " ".join(env.values())
    assert "secret" not in " ".join(env.values())


def test_code_execution_blocks_parent_workspace_path_references():
    from app.services.agent_tool_domains.code_exec import _check_code_safety, _check_command_safety

    assert "directory traversal" in (_check_code_safety("python", "open('../runtime_artifacts/x', 'w')") or "")
    assert "directory traversal" in (_check_code_safety("node", "fs.writeFileSync('../logs/x', 'bad')") or "")
    assert "directory traversal" in (_check_code_safety("bash", "echo bad > ../evolution/x") or "")
    assert "directory traversal" in (_check_command_safety("cat ../runtime_artifacts/session_memory.md") or "")


@pytest.mark.asyncio
async def test_local_provider_sanitizes_caller_env_and_records_evidence(tmp_path: Path, monkeypatch):
    from app.services.code_execution.local_provider import execute_local_sandboxed_command

    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "none")
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "1")

    home = tmp_path / "home"
    work_dir = tmp_path / "work"
    home.mkdir()
    work_dir.mkdir()
    result = await execute_local_sandboxed_command(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('OPENAI_API_KEY', 'missing')); "
            "print(os.environ.get('DATABASE_URL', 'missing'))",
        ],
        work_dir=work_dir,
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "OPENAI_API_KEY": "sk-test",
            "DATABASE_URL": "postgresql://owner-secret",
        },
        timeout=5,
    )

    assert result.error is None
    assert "sk-test" not in result.stdout
    assert "postgresql://owner-secret" not in result.stdout
    assert result.stdout.count("missing") == 2
    assert result.evidence["provider"] == "local_os_sandbox"
    assert result.evidence["isolation"] == "unsandboxed_dev_bypass"
    assert result.evidence["credential_egress"] == "blocked_by_env_allowlist"
    assert result.evidence["env_policy"]["credential_keys_blocked"] == ["DATABASE_URL", "OPENAI_API_KEY"]


@pytest.mark.asyncio
async def test_run_command_returns_tool_envelope_with_code_execution_evidence(tmp_path: Path, monkeypatch):
    from app.services.agent_tool_domains import code_exec
    from app.services.code_execution.contracts import CodeExecutionResult
    from app.tools.result_envelope import ToolContentEnvelope

    async def fake_execute_agent_command(*_args, **_kwargs):
        return CodeExecutionResult(
            stdout="ok\n",
            evidence={
                "provider": "vercel_sandbox",
                "isolation": "vercel_microvm",
                "network_policy": "deny-all",
            },
        )

    monkeypatch.setattr(code_exec, "execute_agent_command", fake_execute_agent_command)

    result = await code_exec._run_command(tmp_path, {"command": "echo ok", "timeout": 5})

    assert isinstance(result, ToolContentEnvelope)
    assert "Output:\nok" in result
    assert result.metadata["code_execution_evidence"]["provider"] == "vercel_sandbox"


@pytest.mark.asyncio
async def test_execute_code_blocks_sandbox_installed_unsafe_skill_before_activation(
    tmp_path: Path,
    monkeypatch,
):
    from app.services.agent_tool_domains import code_exec
    from app.services.code_execution.contracts import CodeExecutionResult

    async def fake_execute_agent_command(_command, *, env, **_kwargs):
        skill_dir = Path(env["HOME"]) / ".agents" / "skills" / "unsafe-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: unsafe\n---\nOPENAI_API_KEY=abcdef123456\n",
            encoding="utf-8",
        )
        return CodeExecutionResult(stdout="ok\n", evidence={"provider": "test"})

    monkeypatch.setattr(code_exec, "execute_agent_command", fake_execute_agent_command)

    result = await code_exec._execute_code(
        tmp_path,
        {
            "language": "python",
            "code": "print('ok')",
            "timeout": 5,
        },
    )

    assert "SkillGuard blocked sandbox-installed skill before activation" in str(result)
    assert not (tmp_path / "skills" / "unsafe-skill" / "SKILL.md").exists()


def test_promote_nested_workspace_artifacts_moves_workspace_prefixed_outputs(tmp_path: Path):
    from app.services.agent_tool_domains.code_exec import _promote_nested_workspace_artifacts

    work_dir = tmp_path / "workspace"
    nested = work_dir / "workspace" / "reports"
    nested.mkdir(parents=True)
    (nested / "bank.xlsx").write_bytes(b"xlsx")

    moved = _promote_nested_workspace_artifacts(work_dir)

    assert moved == ["reports/bank.xlsx"]
    assert (work_dir / "reports" / "bank.xlsx").read_bytes() == b"xlsx"
    assert not (work_dir / "workspace").exists()
