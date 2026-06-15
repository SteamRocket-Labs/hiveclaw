from __future__ import annotations

import io
import tarfile
from uuid import uuid4

import pytest


class _FakeAsyncCommandFinished:
    def __init__(self, *, stdout: str = "", stderr: str = "", exit_code: int = 0):
        self.exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr

    async def stdout(self) -> str:
        return self._stdout

    async def stderr(self) -> str:
        return self._stderr


class _FakeVercelSandbox:
    created: list[dict] = []
    instances: list["_FakeVercelSandbox"] = []
    # toggled per test: simulate `npx skills add` writing under $HOME/.agents
    home_has_skill: bool = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.files: dict[str, bytes] = {}
        self.commands: list[dict] = []
        self.stopped = False

    @classmethod
    async def create(cls, **kwargs):
        sandbox = cls(**kwargs)
        cls.created.append(kwargs)
        cls.instances.append(sandbox)
        return sandbox

    async def write_files(self, files):
        # Real WriteFile is a TypedDict, so subscript access matches production.
        for item in files:
            self.files[item["path"]] = item["content"]

    async def run_command(self, cmd: str, args=None, *, cwd: str | None = None, env=None, sudo: bool = False):
        args = list(args or [])
        self.commands.append({"cmd": cmd, "args": args, "cwd": cwd, "env": env or {}, "sudo": sudo})
        # The real user command under test.
        if cmd in {"bash", "python3", "node"} and any("printf hi" in a or "_exec_tmp" in a for a in args):
            return _FakeAsyncCommandFinished(stdout="hi")
        # Workspace sync-back: produce the output archive.
        if cmd == "tar" and "-czf" in args and any("workspace-out.tar.gz" in a for a in args):
            self.files["workspace-out.tar.gz"] = _tar_bytes({"out.txt": b"hi"})
        # HOME/.agents sync-back: produce the home archive only when a skill "installed".
        if cmd == "sh" and args[:1] == ["-lc"] and "agent-home-out.tar.gz" in args[-1]:
            if type(self).home_has_skill:
                self.files["agent-home-out.tar.gz"] = _tar_bytes({".agents/skills/demo/SKILL.md": b"# demo"})
        return _FakeAsyncCommandFinished()

    async def read_file(self, path: str, *, cwd: str | None = None):
        return self.files.get(path)

    async def stop(self, **_kwargs):
        self.stopped = True


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _set_vercel_env(monkeypatch, policy: str = "deny-all"):
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "vercel_sandbox")
    monkeypatch.setenv("HIVE_CODE_EXEC_NETWORK_POLICY", policy)
    monkeypatch.setenv("VERCEL_TEAM_ID", "team_test")
    monkeypatch.setenv("VERCEL_PROJECT_ID", "prj_test")
    monkeypatch.setenv("VERCEL_TOKEN", "token_test")


@pytest.fixture
def fake_vercel(monkeypatch):
    from app.services.code_execution import vercel_provider

    _FakeVercelSandbox.created.clear()
    _FakeVercelSandbox.instances.clear()
    _FakeVercelSandbox.home_has_skill = False
    monkeypatch.setattr(vercel_provider, "AsyncSandbox", _FakeVercelSandbox)
    return _FakeVercelSandbox


@pytest.mark.asyncio
async def test_run_command_uses_vercel_sandbox_provider_and_syncs_workspace(tmp_path, monkeypatch, fake_vercel):
    from app.services.agent_tool_domains.code_exec import _run_command

    _set_vercel_env(monkeypatch)
    workspace_root = tmp_path / str(uuid4())
    result = await _run_command(workspace_root, {"command": "printf hi > out.txt; cat out.txt", "timeout": 5})

    assert "Output:\nhi" in result
    assert (workspace_root / "workspace" / "out.txt").read_text(encoding="utf-8") == "hi"
    assert fake_vercel.created[0]["team_id"] == "team_test"
    assert fake_vercel.created[0]["network_policy"] == "deny-all"
    assert fake_vercel.instances[0].stopped is True


@pytest.mark.asyncio
async def test_create_timeout_is_milliseconds_not_seconds(tmp_path, monkeypatch, fake_vercel):
    """Regression: SDK create(timeout=) is milliseconds; seconds-as-ms expires instantly."""
    from app.services.agent_tool_domains.code_exec import _run_command

    _set_vercel_env(monkeypatch)
    await _run_command(tmp_path / str(uuid4()), {"command": "printf hi > out.txt; cat out.txt", "timeout": 50})

    # vercel_provider: max(timeout + 30, 60) * 1000 -> (50 + 30) * 1000 = 80000ms (NOT 80s)
    assert fake_vercel.created[0]["timeout"] == 80000


@pytest.mark.asyncio
async def test_execute_code_uses_vercel_sandbox(tmp_path, monkeypatch, fake_vercel):
    from app.services.agent_tool_domains.code_exec import _execute_code

    _set_vercel_env(monkeypatch)
    workspace_root = tmp_path / str(uuid4())
    result = await _execute_code(workspace_root, {"language": "python", "code": "print('x')", "timeout": 5})

    assert "❌" not in result
    assert fake_vercel.created[0]["runtime"] == "python3.13"
    assert fake_vercel.instances[0].stopped is True


@pytest.mark.asyncio
async def test_npx_skills_artifacts_synced_back_from_remote_home(tmp_path, monkeypatch, fake_vercel):
    """Verify ③: skills installed under remote $HOME/.agents are tarred back to exec_home."""
    from app.services.code_execution.vercel_provider import execute_vercel_sandbox_command

    _set_vercel_env(monkeypatch)
    fake_vercel.home_has_skill = True
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    exec_home = tmp_path / "exec_home"
    exec_home.mkdir()

    result = await execute_vercel_sandbox_command(
        ["bash", "-lc", "npx skills add demo -y"],
        work_dir=work_dir,
        env={"HOME": str(exec_home)},
        timeout=120,
        runtime="node24",
        network_policy="allow-all",
    )

    assert result.error is None
    assert fake_vercel.created[0]["network_policy"] == "allow-all"
    assert fake_vercel.created[0]["runtime"] == "node24"
    # the skill landed back in the caller's local HOME, where hr.py harvests it
    assert (exec_home / ".agents" / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "# demo"


@pytest.mark.asyncio
async def test_network_policy_is_threaded_through_service(tmp_path, monkeypatch, fake_vercel):
    from app.services.code_execution.service import execute_agent_command

    _set_vercel_env(monkeypatch, policy="deny-all")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    await execute_agent_command(
        ["bash", "-lc", "echo hi"],
        work_dir=work_dir,
        env={"HOME": str(tmp_path / "h")},
        timeout=10,
        network_policy="allow-all",
    )
    # explicit per-call policy overrides the deny-all env default
    assert fake_vercel.created[0]["network_policy"] == "allow-all"


@pytest.mark.asyncio
async def test_vercel_provider_sanitizes_caller_env_and_records_evidence(tmp_path, monkeypatch, fake_vercel):
    from app.services.code_execution.vercel_provider import execute_vercel_sandbox_command

    _set_vercel_env(monkeypatch)
    work_dir = tmp_path / "work"
    home = tmp_path / "home"
    work_dir.mkdir()
    home.mkdir()

    result = await execute_vercel_sandbox_command(
        ["bash", "-lc", "printf hi"],
        work_dir=work_dir,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "sk-test",
            "DATABASE_URL": "postgresql://owner-secret",
            "PIP_INDEX_URL": "https://user:token@example.com/simple",
        },
        timeout=10,
    )

    user_command = next(item for item in fake_vercel.instances[0].commands if item["cmd"] == "bash")
    exec_env = user_command["env"]
    assert result.error is None
    assert "OPENAI_API_KEY" not in exec_env
    assert "DATABASE_URL" not in exec_env
    assert exec_env["PIP_INDEX_URL"] == "https://example.com/simple"
    assert exec_env["HOME"] == "/vercel/sandbox/agent-home"
    assert result.evidence["provider"] == "vercel_sandbox"
    assert result.evidence["isolation"] == "vercel_microvm"
    assert result.evidence["network_policy"] == "deny-all"
    assert result.evidence["credential_egress"] == "blocked_by_env_allowlist"
    assert result.evidence["env_policy"]["credential_keys_blocked"] == ["DATABASE_URL", "OPENAI_API_KEY"]


@pytest.mark.asyncio
async def test_vercel_provider_brokers_credentials_as_handles_not_values(tmp_path, monkeypatch, fake_vercel):
    from app.services.code_execution.vercel_provider import execute_vercel_sandbox_command

    _set_vercel_env(monkeypatch, policy="allowlist:pypi.org,files.pythonhosted.org")
    monkeypatch.setenv("HIVE_CODE_EXEC_BROKERED_CREDENTIAL_KEYS", "TAVILY_API_KEY,EXA_API_KEY")
    work_dir = tmp_path / "work"
    home = tmp_path / "home"
    work_dir.mkdir()
    home.mkdir()

    result = await execute_vercel_sandbox_command(
        ["bash", "-lc", "printf hi"],
        work_dir=work_dir,
        env={"HOME": str(home), "TAVILY_API_KEY": "real-secret"},
        timeout=10,
    )

    user_command = next(item for item in fake_vercel.instances[0].commands if item["cmd"] == "bash")
    exec_env = user_command["env"]
    assert result.error is None
    assert exec_env["HIVE_BROKERED_CREDENTIALS"] == "TAVILY_API_KEY=cred://agent/tavily_api_key"
    assert "real-secret" not in str(exec_env)
    assert result.evidence["credential_broker"]["brokered_keys"] == ["TAVILY_API_KEY"]
    assert result.evidence["network_allowlist"] == ["files.pythonhosted.org", "pypi.org"]


@pytest.mark.asyncio
async def test_vercel_provider_missing_credentials_fails_closed_without_local_fallback(tmp_path, monkeypatch):
    from app.services.agent_tool_domains.code_exec import _run_command

    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "vercel_sandbox")
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "none")
    monkeypatch.delenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", raising=False)

    result = await _run_command(tmp_path, {"command": "pwd", "timeout": 5})

    assert "Vercel Sandbox unavailable" in result
    assert "HIVE_ALLOW_UNSANDBOXED_CODE_EXEC" not in result
