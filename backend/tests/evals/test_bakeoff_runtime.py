from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from time import sleep
from types import ModuleType, SimpleNamespace

import pytest


_FAKE_HERMES_ACCESS_TOKEN = "j4-test-access-token-not-a-real-secret"


def _expected_hermes_attempt_environment(state_root: Path) -> dict[str, str]:
    directories = {
        "HERMES_HOME": state_root / "hermes-home",
        "HOME": state_root / "os-home",
        "CODEX_HOME": state_root / "codex-home",
        "HERMES_MANAGED_DIR": state_root / "managed",
        "TMPDIR": state_root / "tmp",
    }
    temporary = str(directories["TMPDIR"].resolve())
    return {
        **{name: str(path.resolve()) for name, path in directories.items()},
        "TMP": temporary,
        "TEMP": temporary,
        "HERMES_SAFE_MODE": "1",
        "HERMES_IGNORE_USER_CONFIG": "1",
        "HERMES_IGNORE_RULES": "1",
        "HERMES_BUNDLED_SKILLS": str((state_root / "nonexistent-bundled-skills").resolve()),
        "PYTHONNOUSERSITE": "1",
    }


def _hermes_attempt_environment(state_root: Path) -> dict[str, str]:
    values = _expected_hermes_attempt_environment(state_root)
    for name in ("HERMES_HOME", "HOME", "CODEX_HOME", "HERMES_MANAGED_DIR", "TMPDIR"):
        Path(values[name]).mkdir(parents=True, exist_ok=True)
    return values


def test_build_runtime_command_for_claude_code_uses_headless_json_mode(tmp_path: Path) -> None:
    from app.evals.bakeoff_runtime import build_runtime_command

    command = build_runtime_command(
        "claude_code",
        prompt="Return a JSON object.",
        workspace_dir=tmp_path,
    )

    assert command[0] == "claude"
    assert "-p" in command
    assert "--output-format" in command
    assert command[command.index("--output-format") + 1] == "json"
    assert "--dangerously-skip-permissions" in command
    assert "--add-dir" in command
    assert str(tmp_path) in command
    assert "--effort" in command
    assert command[command.index("--effort") + 1] == "low"
    assert "--model" in command
    assert command[command.index("--model") + 1] == "sonnet"


def test_build_runtime_command_for_hermes_uses_single_query_mode(tmp_path: Path) -> None:
    from app.evals.bakeoff_runtime import build_runtime_command

    command = build_runtime_command(
        "hermes_agent",
        prompt="Return a JSON object.",
        workspace_dir=tmp_path,
    )

    assert command[:4] == ["hermes", "chat", "-q", "Return a JSON object."]
    assert "-Q" in command
    assert "--yolo" in command
    assert "--max-turns" in command


def test_extract_runtime_payload_handles_claude_nested_result_json() -> None:
    from app.evals.bakeoff_runtime import extract_runtime_payload

    payload = extract_runtime_payload(
        "claude_code",
        '{"type":"result","subtype":"success","is_error":false,"result":"{\\"status\\":\\"success\\",\\"answer\\":\\"fixed\\"}"}',
    )

    assert payload == {"status": "success", "answer": "fixed"}


def test_extract_runtime_payload_handles_claude_structured_output() -> None:
    from app.evals.bakeoff_runtime import extract_runtime_payload

    payload = extract_runtime_payload(
        "claude_code",
        '{"type":"result","subtype":"success","is_error":false,"result":"","structured_output":{"status":"success","answer":"fixed","evidence":["calculator.py"],"files_created":[],"used_parallelism":false,"notes":"done"}}',
    )

    assert payload == {
        "status": "success",
        "answer": "fixed",
        "evidence": ["calculator.py"],
        "files_created": [],
        "used_parallelism": False,
        "notes": "done",
    }


def test_run_runtime_bakeoff_reports_unavailable_when_auth_is_missing(monkeypatch, tmp_path: Path) -> None:
    """Spec §2.4: a runtime that cannot run yields an honest empty report —
    the repo-evidence fake-score fallback is retired."""
    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(runtime, "_which", lambda executable: f"/tmp/{executable}")
    monkeypatch.setattr(
        runtime,
        "_run_process",
        lambda command, cwd, timeout_seconds: runtime.ProcessRunResult(
            command=command,
            cwd=str(cwd),
            returncode=1,
            stdout='{"type":"result","subtype":"success","is_error":true,"result":"Not logged in · Please run /login"}',
            stderr="",
            duration_ms=42,
        ),
    )

    report = runtime.run_runtime_bakeoff("claude_code", output_dir=tmp_path)

    assert report["transport"] == "runtime_unavailable"
    assert report["runtime"]["status"] == "auth_required"
    assert report["auth_status"] == "auth_required"
    assert report["benchmark_complete"] is False
    assert report["scenarios"] == {}
    assert "fallback" not in report
    assert isinstance(report["artifact_paths"], list)


def test_write_runtime_artifacts_handles_byte_outputs(tmp_path: Path) -> None:
    from app.evals.bakeoff_runtime import ProcessRunResult, ScenarioWorkspace, _write_runtime_artifacts

    scenario = ScenarioWorkspace(
        name="coding",
        workspace_dir=tmp_path / "workspace",
        prompt="prompt",
        rubric="rubric",
    )
    scenario.workspace_dir.mkdir(parents=True, exist_ok=True)
    result = ProcessRunResult(
        command=["hermes"],
        cwd=str(tmp_path),
        returncode=124,
        stdout=b"partial output",  # type: ignore[arg-type]
        stderr=b"timeout stderr",  # type: ignore[arg-type]
        duration_ms=1234,
    )

    _write_runtime_artifacts(tmp_path, scenario, "prompt text", result)

    assert (tmp_path / "runtime" / "coding" / "stdout.txt").read_text(encoding="utf-8") == "partial output"
    assert (tmp_path / "runtime" / "coding" / "stderr.txt").read_text(encoding="utf-8") == "timeout stderr"


def test_run_runtime_bakeoff_scores_timeout_from_workspace_artifacts(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(runtime, "_SCENARIOS", ("coding",))
    monkeypatch.setattr(runtime, "_which", lambda executable: f"/tmp/{executable}")
    monkeypatch.setattr(runtime, "_preflight_runtime", lambda target, output_dir, profile: None)

    def fake_run(command, cwd, timeout_seconds):
        workspace = Path(cwd)
        (workspace / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return runtime.ProcessRunResult(
            command=command,
            cwd=str(cwd),
            returncode=124,
            stdout="partial transcript before timeout",
            stderr=f"Timed out after {timeout_seconds} seconds.",
            duration_ms=timeout_seconds * 1000,
        )

    monkeypatch.setattr(runtime, "_run_process", fake_run)

    report = runtime.run_runtime_bakeoff("hermes_agent", output_dir=tmp_path)

    scenario = report["scenarios"]["coding"]
    assert scenario["score"] == 80
    assert scenario["ready"] is True
    assert scenario["score_breakdown"]["timeout"] is True
    assert scenario["score_breakdown"]["reason"] == "timeout_partial"
    assert report["transport"] == "live_cli_partial"
    assert report["benchmark_complete"] is False
    assert report["runtime"]["status"] == "partial"
    assert report["incomplete_scenarios"] == [{"scenario": "coding", "reason": "timeout_partial"}]


def test_run_runtime_bakeoff_uses_hermes_runtime_profile(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    captured: dict[str, object] = {}

    monkeypatch.setattr(runtime, "_SCENARIOS", ("coding",))
    monkeypatch.setattr(runtime, "_which", lambda executable: f"/tmp/{executable}")
    monkeypatch.setattr(runtime, "_preflight_runtime", lambda target, output_dir, profile: None)

    def fake_run(command, cwd, timeout_seconds):
        captured["command"] = command
        captured["timeout_seconds"] = timeout_seconds
        return runtime.ProcessRunResult(
            command=command,
            cwd=str(cwd),
            returncode=124,
            stdout="",
            stderr=f"Timed out after {timeout_seconds} seconds.",
            duration_ms=timeout_seconds * 1000,
        )

    monkeypatch.setattr(runtime, "_run_process", fake_run)

    runtime.run_runtime_bakeoff("hermes_agent", output_dir=tmp_path)

    command = captured["command"]
    assert isinstance(command, list)
    assert captured["timeout_seconds"] == 90
    assert command[command.index("--max-turns") + 1] == "6"


def test_run_runtime_bakeoff_live_cli_marks_benchmark_complete(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(runtime, "_SCENARIOS", ("coding",))
    monkeypatch.setattr(runtime, "_which", lambda executable: f"/tmp/{executable}")
    monkeypatch.setattr(runtime, "_preflight_runtime", lambda target, output_dir, profile: None)

    def fake_run(command, cwd, timeout_seconds):
        workspace = Path(cwd)
        (workspace / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return runtime.ProcessRunResult(
            command=command,
            cwd=str(cwd),
            returncode=0,
            stdout='{"status":"success","answer":"fixed","evidence":["calculator.py"],"files_created":["calculator.py"],"used_parallelism":false,"notes":"done"}',
            stderr="",
            duration_ms=1200,
        )

    monkeypatch.setattr(runtime, "_run_process", fake_run)

    report = runtime.run_runtime_bakeoff("hermes_agent", output_dir=tmp_path)

    assert report["transport"] == "live_cli"
    assert report["benchmark_complete"] is True
    assert report["auth_status"] == "ok"
    assert report["runtime"]["status"] == "completed"
    assert report["artifact_paths"]


def test_run_runtime_bakeoff_uses_scenario_prompt_without_workspace_suffix(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    captured: dict[str, object] = {}

    monkeypatch.setattr(runtime, "_SCENARIOS", ("coding",))
    monkeypatch.setattr(runtime, "_which", lambda executable: f"/tmp/{executable}")
    monkeypatch.setattr(runtime, "_preflight_runtime", lambda target, output_dir, profile: None)

    def fake_run(command, cwd, timeout_seconds):
        captured["command"] = command
        workspace = Path(cwd)
        (workspace / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return runtime.ProcessRunResult(
            command=command,
            cwd=str(cwd),
            returncode=0,
            stdout='{"structured_output":{"status":"success","answer":"fixed","evidence":["calculator.py"],"files_created":[],"used_parallelism":false,"notes":"done"}}',
            stderr="",
            duration_ms=1200,
        )

    monkeypatch.setattr(runtime, "_run_process", fake_run)

    runtime.run_runtime_bakeoff("claude_code", output_dir=tmp_path)

    command = captured["command"]
    assert isinstance(command, list)
    prompt = str(command[-1])
    assert "Current scenario:" not in prompt
    assert "Workspace:" not in prompt


def test_run_process_does_not_force_simple_mode_for_claude(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs.get("env")
            captured["start_new_session"] = kwargs.get("start_new_session")

        def communicate(self, timeout=None):
            assert timeout == 5
            return '{"status":"success"}', ""

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)

    runtime._run_process(
        ["claude", "-p", "Return ok"],
        cwd=tmp_path,
        timeout_seconds=5,
    )

    env = captured.get("env")
    assert isinstance(env, dict)
    assert env.get("CLAUDE_CODE_SIMPLE") != "1"
    assert captured["start_new_session"] is (os.name == "posix")


def test_run_process_timeout_terminates_descendant_process_group(tmp_path: Path) -> None:
    if os.name != "posix":
        return

    import app.evals.bakeoff_runtime as runtime

    marker = tmp_path / "escaped-child-marker"
    child_code = (
        "from pathlib import Path; import time; "
        f"time.sleep(2); Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
    )
    parent_code = (
        f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(30)"
    )

    result = runtime._run_process(
        [sys.executable, "-c", parent_code],
        cwd=tmp_path,
        timeout_seconds=1,
    )
    sleep(1.5)

    assert result.returncode == 124
    assert "process group terminated" in result.stderr
    assert not marker.exists()


def test_j4_run_process_fails_closed_when_workspace_sandbox_is_unavailable(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_build_workspace_sandbox_command",
        lambda command, cwd, env: (None, [], {"status": "unavailable", "provider": None, "reason": "missing"}),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unsandboxed command must not run")),
    )

    result = runtime._run_process(
        ["fake-agent"],
        cwd=tmp_path,
        timeout_seconds=5,
        require_workspace_sandbox=True,
    )

    assert result.returncode == 126
    assert result.sandbox == {"status": "unavailable", "provider": None, "reason": "missing"}


def test_j4_workspace_sandbox_adds_only_attempt_scoped_state_root(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime
    from app.services import subprocess_sandbox

    workspace = tmp_path / "attempt" / "workspace"
    state_root = tmp_path / "attempt" / "state"
    outside = tmp_path / "outside"
    for path in (workspace, state_root, outside):
        path.mkdir(parents=True)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        subprocess_sandbox,
        "probe_os_sandbox_capability",
        lambda: SimpleNamespace(available=True, provider="test-sandbox", reason="test"),
    )

    def fake_build(command, *, work_dir, env, spec):
        del env
        captured.update({"command": command, "work_dir": work_dir, "spec": spec})
        return SimpleNamespace(command=["sandbox", *command], cleanup_paths=[], error=None)

    monkeypatch.setattr(subprocess_sandbox, "build_sandboxed_agent_command", fake_build)

    command, _cleanup, receipt = runtime._build_workspace_sandbox_command(
        ["agent"],
        workspace,
        {"HOME": str(tmp_path / "home")},
        additional_writable_roots=(state_root,),
    )

    assert command == ["sandbox", "agent"]
    assert captured["work_dir"] == workspace.resolve()
    assert captured["spec"].writable_roots == (str(workspace.resolve()), str(state_root.resolve()))
    assert captured["spec"].network_access is True
    assert receipt["writable_roots"] == [str(workspace.resolve()), str(state_root.resolve())]

    escaped, _cleanup, escaped_receipt = runtime._build_workspace_sandbox_command(
        ["agent"],
        workspace,
        {"HOME": str(tmp_path / "home")},
        additional_writable_roots=(outside,),
    )
    assert escaped is None
    assert "escapes" in escaped_receipt["reason"]


def test_hermes_j4_launcher_requires_attempt_local_state_and_profile_roots(monkeypatch, tmp_path: Path) -> None:
    import app.evals.hermes_j4_launcher as launcher

    source_root = tmp_path / "source"
    site_packages = tmp_path / "site-packages"
    state_root = tmp_path / "attempt" / "state"
    workspace_root = tmp_path / "attempt" / "workspace"
    source_root.mkdir()
    site_packages.mkdir()
    state_root.mkdir(parents=True)
    workspace_root.mkdir(parents=True)
    state_db = state_root / "state.db"
    called: list[bool] = []
    hermes_state = ModuleType("hermes_state")
    hermes_state.DEFAULT_DB_PATH = tmp_path / "real-home" / "state.db"
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    hermes_main = ModuleType("hermes_cli.main")
    hermes_main.main = lambda: called.append(True)
    tools = ModuleType("tools")
    tools.__path__ = []
    file_tools = ModuleType("tools.file_tools")
    file_tools.read_file_tool = lambda *_args, **_kwargs: ""
    file_tools.search_tool = lambda *_args, **_kwargs: ""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "hermes_state", hermes_state)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.main", hermes_main)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.file_tools", file_tools)
    monkeypatch.setenv(launcher.SOURCE_ROOT_ENV, str(source_root))
    monkeypatch.setenv(launcher.SITE_PACKAGES_ENV, str(site_packages))
    monkeypatch.setenv(launcher.STATE_DB_ENV, str(state_db))
    monkeypatch.setenv(launcher.WORKSPACE_ROOT_ENV, str(workspace_root))
    attempt_env = _hermes_attempt_environment(state_root)
    for name, value in attempt_env.items():
        monkeypatch.setenv(name, value)

    launcher.main()

    assert called == [True]
    assert hermes_state.DEFAULT_DB_PATH == state_db.resolve()
    assert {name: os.environ[name] for name in attempt_env} == attempt_env


def test_hermes_j4_launcher_imports_configured_source_with_real_venv_python(tmp_path: Path) -> None:
    import app.evals.hermes_j4_launcher as launcher

    source_root = tmp_path / "source"
    package_root = source_root / "hermes_cli"
    tools_root = source_root / "tools"
    site_packages = tmp_path / "site-packages"
    state_root = tmp_path / "attempt" / "state"
    workspace_root = tmp_path / "attempt" / "workspace"
    package_root.mkdir(parents=True)
    tools_root.mkdir(parents=True)
    site_packages.mkdir()
    state_root.mkdir(parents=True)
    workspace_root.mkdir(parents=True)
    (source_root / "hermes_state.py").write_text(
        "from pathlib import Path\nDEFAULT_DB_PATH = Path('/unredirected/state.db')\n",
        encoding="utf-8",
    )
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (tools_root / "__init__.py").write_text("", encoding="utf-8")
    (tools_root / "file_tools.py").write_text(
        "def read_file_tool(*args, **kwargs): return ''\ndef search_tool(*args, **kwargs): return ''\n",
        encoding="utf-8",
    )
    (package_root / "main.py").write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "import hermes_state\n"
        "def main():\n"
        '    print(json.dumps({"argv": sys.argv[1:], "module": __file__, '
        '"state_db": str(hermes_state.DEFAULT_DB_PATH), '
        '"hermes_home": os.environ.get("HERMES_HOME")}))\n',
        encoding="utf-8",
    )
    state_db = state_root / "state.db"
    attempt_env = _hermes_attempt_environment(state_root)
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(Path(launcher.__file__).resolve()), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONNOUSERSITE": "1",
            launcher.SOURCE_ROOT_ENV: str(source_root),
            launcher.SITE_PACKAGES_ENV: str(site_packages),
            launcher.STATE_DB_ENV: str(state_db),
            launcher.WORKSPACE_ROOT_ENV: str(workspace_root),
            **attempt_env,
        },
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "argv": ["--version"],
        "module": str(package_root / "main.py"),
        "state_db": str(state_db.resolve()),
        "hermes_home": attempt_env["HERMES_HOME"],
    }


def test_j4_cli_commands_are_argv_only_and_pin_the_same_model(tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "coding")
    envelope, _digest = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    freecode_binary = tmp_path / "bin" / "freecode"
    freecode_hook = tmp_path / "runtime" / "freecode_j4_hook.py"
    hook_python = tmp_path / "runtime" / "python"
    hermes_python = tmp_path / "frozen-hermes" / "python"

    freecode = runtime._freecode_command(
        prompt=scenario.prompt,
        envelope=envelope,
        workspace_root=tmp_path / "workspace",
        binary=freecode_binary,
        hook=freecode_hook,
        hook_python=hook_python,
    )
    hermes = runtime._hermes_command(
        workspace_root=tmp_path / "workspace",
        envelope=envelope,
        python=hermes_python,
    )

    assert freecode[0] == str(freecode_binary)
    assert freecode[freecode.index("--model") + 1] == "gpt-5.4"
    assert freecode[freecode.index("--effort") + 1] == "low"
    assert freecode[freecode.index("--max-turns") + 1] == "6"
    assert freecode[freecode.index("--tools") + 1] == "Read,Write,Edit,Glob,Grep"
    assert freecode[freecode.index("--permission-mode") + 1] == "dontAsk"
    settings = json.loads(freecode[freecode.index("--settings") + 1])
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == (f"{hook_python} -I -S {freecode_hook}")
    assert freecode[freecode.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in freecode
    assert "--disable-slash-commands" in freecode
    assert "--no-session-persistence" in freecode
    assert "--no-chrome" in freecode
    assert "--bare" not in freecode
    assert hermes[:6] == [
        str(hermes_python.resolve()),
        "-I",
        "-S",
        str(runtime.HERMES_J4_LAUNCHER.resolve()),
        "chat",
        "--query-file",
    ]
    assert hermes[hermes.index("--provider") + 1] == "openai-codex"
    assert hermes[hermes.index("--reasoning") + 1] == "low"
    assert hermes[hermes.index("--source") + 1] == "p08-j4"


def test_hermes_state_roots_are_isolated_per_envelope(tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    first_workspace = runtime._runtime_workspace_path(tmp_path, "hermes", "envelope-one")
    second_workspace = runtime._runtime_workspace_path(tmp_path, "hermes", "envelope-two")

    first_state = runtime._hermes_state_root(first_workspace, "envelope-one")
    second_state = runtime._hermes_state_root(second_workspace, "envelope-two")

    assert first_state == tmp_path / "j4_runtime" / "hermes" / "envelope-one" / "state"
    assert second_state == tmp_path / "j4_runtime" / "hermes" / "envelope-two" / "state"
    assert first_state != second_state


def test_j4_missing_freecode_build_manifest_fails_before_runtime_execution(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_run_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unattested runtime must not execute")),
    )
    prepared, blockers = runtime._prepare_j4_runtimes(runtime.J4RuntimeConfig())

    assert prepared is None
    assert blockers == [
        {
            "code": "runtime_identity_attestation_failed",
            "runtime": "freecode",
            "detail": "freecode_build_manifest_attestation_required",
        }
    ]


def test_hermes_runtime_identity_requires_exact_clean_frozen_source(tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    source_root = tmp_path / "hermes-source"
    (source_root / "hermes_cli").mkdir(parents=True)
    (source_root / "pyproject.toml").write_text(
        '[project]\nname = "hermes-agent"\nversion = "0.21.0"\n',
        encoding="utf-8",
    )
    (source_root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (source_root / ".gitignore").write_text("venv/\n", encoding="utf-8")
    (source_root / "hermes_cli" / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    (source_root / "contributors").mkdir()
    (source_root / "contributors" / "owner.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source_root)], check=True)
    subprocess.run(["git", "-C", str(source_root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source_root),
            "-c",
            "user.name=J4 Test",
            "-c",
            "user.email=j4@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    source_identity, source_errors = runtime._git_source_identity(source_root)
    assert source_errors == []

    base_root = tmp_path / "base-python"
    base_bin = base_root / "bin"
    base_bin.mkdir(parents=True)
    base_python = base_bin / "python"
    base_python.write_bytes(b"base-python-bytes")
    base_python.chmod(0o755)
    venv_root = source_root / "venv"
    (venv_root / "bin").mkdir(parents=True)
    (venv_root / "pyvenv.cfg").write_text(
        f"home = {base_bin}\nversion_info = 3.11\n",
        encoding="utf-8",
    )
    hermes_python = venv_root / "bin" / "python"
    hermes_python.write_bytes(b"frozen-python-bytes")
    hermes_python.chmod(0o755)
    python_identity, python_errors = runtime._python_environment_identity(hermes_python)
    assert python_errors == []
    config = runtime.J4RuntimeConfig(
        hermes_python=str(hermes_python),
        hermes_python_sha256=runtime._sha256_file(hermes_python),
        hermes_python_environment_sha256=python_identity["sha256"],
        hermes_source_root=str(source_root),
        hermes_source_revision=source_identity["revision"],
        hermes_source_sha256=source_identity["sha256"],
    )

    identity, errors = runtime._hermes_runtime_identity(config)

    assert errors == []
    assert identity["sha256"] == identity["runtime_sha256"]
    assert identity["revision"] == source_identity["revision"]
    assert identity["components"]["source"] == {
        "root": str(source_root.resolve()),
        "sha256": source_identity["sha256"],
        "revision": source_identity["revision"],
        "clean": True,
        "lock_sha256": runtime._sha256_file(source_root / "uv.lock"),
        "excluded_runtime_roots": [str(venv_root.resolve())],
        "scope": runtime.HERMES_J4_SOURCE_SCOPE,
    }
    assert identity["path"] == str(hermes_python)
    assert identity["components"]["python"] == python_identity
    assert identity["components"]["python"]["kind"] == "file"
    assert identity["components"]["python"]["resolved_path"] == str(hermes_python.resolve())
    assert identity["components"]["python"]["base_python_path"] == str(base_python.resolve())
    assert identity["components"]["python"]["base_python_sha256"] == runtime._sha256_file(base_python)
    assert identity["components"]["launcher"]["path"] == str(runtime.HERMES_J4_LAUNCHER.resolve())

    command = runtime._hermes_command(
        workspace_root=tmp_path / "workspace",
        envelope=runtime._build_same_envelope(
            runtime._scenario_workspace(tmp_path / "seed", "memory_recall"),
            config=runtime.J4RuntimeConfig(),
        )[0],
        python=Path(identity["path"]),
    )
    assert command[0] == str(hermes_python)

    (source_root / "hermes_cli" / "main.py").write_text("def main(): return 1\n", encoding="utf-8")
    dirty_identity, dirty_errors = runtime._hermes_runtime_identity(config)

    assert dirty_identity["runtime_sha256"] == ""
    assert "hermes_source_dirty" in dirty_errors

    (source_root / "hermes_cli" / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    (source_root / "contributors" / "owner.txt").write_text("dirty tracked bytes\n", encoding="utf-8")
    dirty_contributor_identity, dirty_contributor_errors = runtime._hermes_runtime_identity(config)

    assert dirty_contributor_identity["runtime_sha256"] == ""
    assert "hermes_source_dirty" in dirty_contributor_errors

    (source_root / "contributors" / "owner.txt").write_text("original\n", encoding="utf-8")
    (source_root / "contributors" / "untracked.txt").write_text("dirty untracked bytes\n", encoding="utf-8")
    untracked_contributor_identity, untracked_contributor_errors = runtime._hermes_runtime_identity(config)

    assert untracked_contributor_identity["runtime_sha256"] == ""
    assert "hermes_source_dirty" in untracked_contributor_errors


def test_same_envelope_missing_freecode_manifest_preflights_before_any_adapter(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    calls = {name: 0 for name in ("hive", "freecode", "hermes")}

    def must_not_run(runtime_name):
        def run(*args, **kwargs):
            del args, kwargs
            calls[runtime_name] += 1
            raise AssertionError("global runtime preflight must stop every adapter")

        return run

    monkeypatch.setattr(runtime, "_run_hive_j4", must_not_run("hive"))
    monkeypatch.setattr(runtime, "_run_freecode_j4", must_not_run("freecode"))
    monkeypatch.setattr(runtime, "_run_hermes_j4", must_not_run("hermes"))
    config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="test-token",
        hive_agent_id="agent-id",
        hive_revision="revision",
        hive_binary_sha256="a" * 64,
        external_profile_authorized=True,
    )

    report = runtime.run_same_envelope_bakeoff(output_dir=tmp_path, config=config)

    assert calls == {"hive": 0, "freecode": 0, "hermes": 0}
    assert report["receipts"] == []
    assert report["comparison"]["scores"] == {}
    assert report["comparison"]["blockers"] == [
        {
            "code": "runtime_identity_attestation_failed",
            "runtime": "freecode",
            "detail": "freecode_build_manifest_attestation_required",
        }
    ]


def test_same_envelope_unattested_hermes_runtime_preflights_before_any_adapter(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    blocker = {
        "code": "runtime_identity_attestation_failed",
        "runtime": "hermes",
        "detail": "hermes_runtime_attestation_required",
    }
    monkeypatch.setattr(
        runtime,
        "_prepare_j4_runtimes",
        lambda _config: (None, [blocker]),
    )
    calls = {name: 0 for name in ("hive", "freecode", "hermes")}

    def must_not_run(runtime_name):
        def run(*args, **kwargs):
            del args, kwargs
            calls[runtime_name] += 1
            raise AssertionError("global runtime preflight must stop every adapter")

        return run

    monkeypatch.setattr(runtime, "_run_hive_j4", must_not_run("hive"))
    monkeypatch.setattr(runtime, "_run_freecode_j4", must_not_run("freecode"))
    monkeypatch.setattr(runtime, "_run_hermes_j4", must_not_run("hermes"))
    report = runtime.run_same_envelope_bakeoff(
        output_dir=tmp_path,
        config=runtime.J4RuntimeConfig(
            hive_base_url="https://hive.example",
            hive_bearer="test-token",
            hive_agent_id="agent-id",
            hive_revision="revision",
            hive_binary_sha256="a" * 64,
            external_profile_authorized=True,
        ),
    )

    assert calls == {"hive": 0, "freecode": 0, "hermes": 0}
    assert report["comparison"]["blockers"] == [blocker]


def test_same_envelope_failed_hermes_launcher_smoke_preflights_before_any_adapter(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    hermes_python = tmp_path / "venv" / "bin" / "python"
    hermes_source = tmp_path / "hermes-source"
    site_packages = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    site_packages.mkdir(parents=True)
    identity = {
        "path": str(hermes_python),
        "version": "Hermes Agent v0.21.0",
        "components": {
            "launcher": {"path": str(runtime.HERMES_J4_LAUNCHER.resolve())},
            "source": {"root": str(hermes_source)},
        },
    }
    smoke_calls: list[dict[str, object]] = []

    def failed_smoke(
        command,
        cwd,
        timeout_seconds,
        *,
        env_overrides=None,
        env_remove=(),
        require_workspace_sandbox=False,
        additional_writable_roots=(),
        network_access=True,
    ):
        del timeout_seconds
        smoke_calls.append(
            {
                "command": command,
                "cwd": cwd,
                "env_overrides": env_overrides,
                "env_remove": env_remove,
                "require_workspace_sandbox": require_workspace_sandbox,
                "additional_writable_roots": additional_writable_roots,
                "network_access": network_access,
            }
        )
        return runtime.ProcessRunResult(
            command=command,
            cwd=str(cwd),
            returncode=1,
            stdout="",
            stderr="import failed",
            duration_ms=1,
            sandbox={"status": "enforced", "provider": "test", "reason": "test"},
        )

    monkeypatch.setattr(runtime, "_run_process", failed_smoke)
    monkeypatch.setattr(
        runtime,
        "_prepare_j4_runtimes",
        lambda _config: (
            None,
            [
                {
                    "code": "runtime_smoke_failed",
                    "runtime": "hermes",
                    "detail": ",".join(runtime._hermes_launcher_smoke(identity, site_packages=site_packages)),
                }
            ],
        ),
    )
    adapter_calls = {name: 0 for name in ("hive", "freecode", "hermes")}

    def must_not_run(runtime_name):
        def run(*args, **kwargs):
            del args, kwargs
            adapter_calls[runtime_name] += 1
            raise AssertionError("failed Hermes smoke must stop every adapter")

        return run

    monkeypatch.setattr(runtime, "_run_hive_j4", must_not_run("hive"))
    monkeypatch.setattr(runtime, "_run_freecode_j4", must_not_run("freecode"))
    monkeypatch.setattr(runtime, "_run_hermes_j4", must_not_run("hermes"))

    report = runtime.run_same_envelope_bakeoff(
        output_dir=tmp_path / "output",
        config=runtime.J4RuntimeConfig(
            hive_base_url="https://hive.example",
            hive_bearer="test-token",
            hive_agent_id="agent-id",
            hive_revision="revision",
            hive_binary_sha256="a" * 64,
            hermes_source_root=str(hermes_source),
            external_profile_authorized=True,
        ),
    )

    assert adapter_calls == {"hive": 0, "freecode": 0, "hermes": 0}
    assert report["comparison"]["blockers"] == [
        {
            "code": "runtime_smoke_failed",
            "runtime": "hermes",
            "detail": "hermes_launcher_runtime_attestation_failed",
        }
    ]
    assert len(smoke_calls) == 1
    smoke = smoke_calls[0]
    assert smoke["command"] == [
        str(hermes_python),
        "-I",
        "-S",
        str(runtime.HERMES_J4_LAUNCHER.resolve()),
        "attest-runtime",
        "--expected-version",
        "0.21.0",
    ]
    assert smoke["require_workspace_sandbox"] is True
    assert smoke["network_access"] is False
    assert smoke["env_remove"] == runtime._HERMES_J4_AMBIENT_ENV_DENYLIST
    assert smoke["env_overrides"][runtime.HERMES_J4_SOURCE_ROOT_ENV] == str(hermes_source)
    assert smoke["env_overrides"][runtime.HERMES_J4_SITE_PACKAGES_ENV] == str(site_packages)
    assert smoke["env_overrides"][runtime.HERMES_J4_WORKSPACE_ROOT_ENV] == str(smoke["cwd"])
    assert Path(smoke["env_overrides"][runtime.HERMES_J4_STATE_DB_ENV]).parent == Path(
        smoke["additional_writable_roots"][0]
    )


def test_hive_base_url_requires_https_except_for_literal_loopback() -> None:
    import app.evals.bakeoff_runtime as runtime

    assert runtime._valid_hive_base_url("https://hive.example") is True
    assert runtime._valid_hive_base_url("http://localhost:8000") is True
    assert runtime._valid_hive_base_url("http://127.0.0.1:8000") is True
    assert runtime._valid_hive_base_url("http://[::1]:8000") is True
    assert runtime._valid_hive_base_url("http://hive.example") is False
    assert runtime._valid_hive_base_url("https://user:secret@hive.example") is False


def test_hive_transcript_attestation_requires_actual_provider_call_and_preserves_terminal_state() -> None:
    import app.evals.bakeoff_runtime as runtime

    events = [
        {
            "scope": {"run_id": "run-1"},
            "payload": {
                "metadata": {
                    "event_type": "model_route",
                    "selected_model": "openai/gpt-5.4",
                    "selected_provider": "openai-response",
                    "model_routing_locked": True,
                    "fallback_model": None,
                }
            },
        },
        {
            "scope": {"run_id": "run-1"},
            "payload": {
                "metadata": {
                    "event_type": "provider_call_ledger",
                    "provider_prompt_ledger": {
                        "provider_call_id": "provider-call-1",
                        "provider": "openai-response",
                        "model": "gpt-5.4",
                        "projected_input_tokens": 81,
                        "tool_names": list(runtime._J4_ALLOWED_TOOLS["hive"]),
                        "tool_schema_sha256": "a" * 64,
                    },
                    "cache_metrics": {"total_input_tokens": 80},
                    "tool_count": 5,
                }
            },
        },
        {
            "kind": "run.needs_reconciliation",
            "lifecycle": "needs_reconciliation",
            "scope": {"run_id": "run-1"},
            "payload": {},
        },
    ]

    attestation = runtime._hive_transcript_attestation(events, run_id="run-1")

    assert attestation["terminal_status"] == "needs_reconciliation"
    assert attestation["call_count"] == 1
    assert attestation["turns"] == 1
    assert attestation["tokens"] == 80
    assert attestation["provider_calls"] == [
        {
            "provider_call_id": "provider-call-1",
            "provider": "openai-response",
            "model": "gpt-5.4",
            "tool_count": 5,
            "tool_names": list(runtime._J4_ALLOWED_TOOLS["hive"]),
            "tool_schema_sha256": "a" * 64,
        }
    ]

    no_call = runtime._hive_transcript_attestation(events[:1], run_id="run-1")
    assert no_call["call_count"] == 0
    assert no_call["terminal_status"] is None

    unattributed = runtime._hive_transcript_attestation(
        [
            *events,
            {
                "scope": {"run_id": "run-1"},
                "payload": {
                    "metadata": {
                        "event_type": "provider_call_ledger",
                        "provider_prompt_ledger": {"provider_call_id": "provider-call-2"},
                    }
                },
            },
        ],
        run_id="run-1",
    )
    assert unattributed["provider_call_event_count"] == 2
    assert unattributed["invalid_provider_call_count"] == 1

    duplicate = runtime._hive_transcript_attestation([*events, events[1]], run_id="run-1")
    assert duplicate["provider_call_event_count"] == 2
    assert duplicate["invalid_provider_call_count"] == 1
    assert duplicate["call_count"] == 1

    unscoped = runtime._hive_transcript_attestation(
        [
            {**events[0], "scope": {}},
            {**events[1], "scope": {}},
            {**events[2], "scope": {}},
        ],
        run_id="run-1",
    )
    assert unscoped["route"] is None
    assert unscoped["call_count"] == 0
    assert unscoped["terminal_status"] is None


def test_hive_cancel_fence_requires_terminal_transcript_and_no_active_run(monkeypatch) -> None:
    import app.evals.bakeoff_runtime as runtime

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, *, active_payload, cancel_payload=None):
            self.active_payload = active_payload
            self.cancel_payload = cancel_payload or {
                "status": "accepted",
                "command_id": "cancel-command-1",
                "run_id": "run-1",
                "accepted": True,
            }
            self.requests = []

        def request(self, method, url, **kwargs):
            self.requests.append({"method": method, "url": url, **kwargs})
            if method == "POST" and url.endswith("/runs/run-1/cancel"):
                return FakeResponse(self.cancel_payload)
            if method == "GET" and url.endswith("/transcript"):
                return FakeResponse(
                    [
                        {
                            "kind": "run.cancelled",
                            "lifecycle": "cancelled",
                            "scope": {"run_id": "run-1"},
                            "payload": {},
                        }
                    ]
                )
            if method == "GET" and url.endswith("/runs/active"):
                return FakeResponse(self.active_payload)
            raise AssertionError(f"unexpected request: {method} {url}")

    settled = FakeClient(active_payload=None)
    status, _events, attestation, cancellation = runtime._hive_cancel_and_fence(
        settled,
        base_url="https://hive.example",
        bearer="secret",
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        attempt_id="attempt-1",
        fence_seconds=1,
    )

    assert status == "cancelled"
    assert attestation["terminal_status"] == "cancelled"
    assert cancellation["fence"] == "settled"
    cancel_request = settled.requests[0]
    assert cancel_request["headers"]["Idempotency-Key"] == "p08-j4-cancel:attempt-1:run-1"
    assert "secret" not in json.dumps(cancellation)

    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(runtime, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runtime, "sleep", lambda _seconds: None)
    still_active = FakeClient(active_payload={"run_id": "run-1"})
    status, _events, attestation, cancellation = runtime._hive_cancel_and_fence(
        still_active,
        base_url="https://hive.example",
        bearer="secret",
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        attempt_id="attempt-1",
        fence_seconds=1,
    )

    assert status == "needs_reconciliation"
    assert attestation["terminal_status"] == "cancelled"
    assert cancellation["fence"] == "unreconciled"

    mismatched = FakeClient(
        active_payload=None,
        cancel_payload={
            "status": "accepted",
            "command_id": "cancel-command-2",
            "run_id": "another-run",
            "accepted": True,
        },
    )
    status, events, attestation, cancellation = runtime._hive_cancel_and_fence(
        mismatched,
        base_url="https://hive.example",
        bearer="secret",
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        attempt_id="attempt-1",
        fence_seconds=1,
    )
    assert status == "needs_reconciliation"
    assert events == []
    assert attestation is None
    assert cancellation["fence"] == "unreconciled"
    assert len(mismatched.requests) == 1


def test_j4_acceptance_requires_every_criterion_and_hive_not_weaker() -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario_scores = {
        "coding": {
            "hive": {
                "ready": False,
                "criteria": {"coding.ast_add": True, "coding.execution_assertions": False},
            },
            "freecode": {
                "ready": True,
                "criteria": {"coding.ast_add": True, "coding.execution_assertions": True},
            },
            "hermes": {
                "ready": False,
                "criteria": {"coding.ast_add": True, "coding.execution_assertions": False},
            },
        }
    }

    decision = runtime._j4_acceptance_decision(scenario_scores)

    assert decision["acceptance_ready"] is False
    assert decision["all_hard_criteria_ready"] is False
    assert decision["hive_not_weaker"] is False
    assert decision["comparisons"]["coding.coding.execution_assertions"] == {
        "hive": False,
        "freecode": True,
        "hermes": False,
        "hive_not_weaker": False,
    }
    scenario_scores["coding"]["hive"]["ready"] = True
    scenario_scores["coding"]["hive"]["criteria"].pop("coding.execution_assertions")
    assert runtime._j4_acceptance_decision(scenario_scores)["all_hard_criteria_ready"] is False


def _j4_success_payload(*, output_file: str = "memory_answer.md") -> dict[str, object]:
    return {
        "status": "success",
        "answer": "done",
        "evidence": [output_file],
        "files_created": [output_file],
        "used_parallelism": False,
        "notes": "done",
    }


def _prepared_j4_runtimes(module, tmp_path: Path):
    fixture_root = tmp_path / "prepared-j4"
    artifact = fixture_root / "freecode"
    hermes_venv_python = fixture_root / "venv" / "bin" / "python"
    hermes_base_python = fixture_root / "base-python" / "bin" / "python"
    site_packages = fixture_root / "venv" / "lib" / "python3.11" / "site-packages"
    hermes_source = fixture_root / "source"
    artifact.parent.mkdir(parents=True)
    hermes_venv_python.parent.mkdir(parents=True)
    hermes_base_python.parent.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    hermes_source.mkdir()
    artifact.write_bytes(b"freecode-test-runtime")
    artifact.chmod(0o755)
    hermes_venv_python.write_bytes(b"hermes-test-venv-python")
    hermes_venv_python.chmod(0o755)
    hermes_base_python.write_bytes(b"hermes-test-base-python")
    hermes_base_python.chmod(0o755)
    (hermes_venv_python.parent.parent / "pyvenv.cfg").write_text(
        f"home = {hermes_base_python.parent}\nversion_info = 3.11\n",
        encoding="utf-8",
    )
    python_sha = module._sha256_file(hermes_venv_python)
    base_python_sha = module._sha256_file(hermes_base_python)
    base_python_environment, base_python_environment_errors = module._tree_identity(hermes_base_python.parent.parent)
    assert base_python_environment_errors == []
    auth_source = fixture_root / "source-auth.json"
    provider_state = {
        "tokens": {
            "access_token": _FAKE_HERMES_ACCESS_TOKEN,
            "refresh_token": "j4-test-refresh-token-not-a-real-secret",
        },
        "auth_mode": "chatgpt",
    }
    pool_entry = {
        "id": "j4-test-credential",
        "auth_type": "oauth",
        "access_token": _FAKE_HERMES_ACCESS_TOKEN,
        "refresh_token": "j4-test-refresh-token-not-a-real-secret",
        "last_status": "ok",
    }
    auth_store = {
        "version": 1,
        "providers": {"openai-codex": provider_state},
        "credential_pool": {"openai-codex": [pool_entry]},
        "active_provider": "openai-codex",
    }
    auth_projection = module._hermes_auth_projection(pool_entry)
    auth_source.write_text(module._canonical_json(auth_store) + "\n", encoding="utf-8")
    auth_source.chmod(0o600)
    auth_run_nonce = b"j4-test-auth-run-nonce"
    auth_profile = {
        "provider": "openai-codex",
        "source_attested": True,
        "expires_at": 4_102_444_800,
        "refresh_capable": True,
        "credential_count": 1,
        "projection_run_sha256": module._sha256_bytes(
            auth_run_nonce + module._canonical_json(auth_projection).encode("utf-8")
        ),
    }
    freecode_input_manifest = {
        "schema": module.FREECODE_BUILD_MANIFEST_SCHEMA,
        "source": {"revision": "freecode-revision", "sha256": "1" * 64},
    }
    freecode_build_receipt = {
        "schema": "hive.j4.freecode_fresh_build_receipt.v1",
        "source_revision": "freecode-revision",
        "artifact": {
            "path": str(artifact),
            "version": "free-code 1.0",
            "size": artifact.stat().st_size,
            "sha256": module._sha256_file(artifact),
        },
        "inputs_stable": True,
        "network_access": False,
    }
    freecode_manifest = {
        **freecode_input_manifest,
        "artifact": dict(freecode_build_receipt["artifact"]),
        "build": {"fresh_receipt_sha256": module._sha256_json(freecode_build_receipt)},
    }
    return module.PreparedJ4Runtimes(
        freecode_manifest=freecode_manifest,
        freecode_input_manifest=freecode_input_manifest,
        freecode_manifest_sha256="2" * 64,
        freecode_build_receipt=freecode_build_receipt,
        freecode_build_receipt_sha256=module._sha256_json(freecode_build_receipt),
        freecode_hook=module.FREECODE_J4_HOOK.resolve(),
        freecode_hook_sha256=module._sha256_file(module.FREECODE_J4_HOOK.resolve()),
        freecode_hook_python=hermes_base_python,
        freecode_hook_python_sha256=base_python_sha,
        freecode_hook_python_environment_sha256=base_python_environment["sha256"],
        hermes_binary={
            "path": str(hermes_base_python),
            "version": "Hermes Agent v0.21.0",
            "sha256": "3" * 64,
            "revision": "hermes-revision",
            "runtime_sha256": "3" * 64,
            "components": {
                "python": {
                    "path": str(hermes_venv_python),
                    "kind": "file",
                    "entry_sha256": python_sha,
                    "link_target": None,
                    "resolved_path": str(hermes_venv_python),
                    "resolved_sha256": python_sha,
                    "venv_root": str(hermes_venv_python.parent.parent),
                    "pyvenv_sha256": module._sha256_file(hermes_venv_python.parent.parent / "pyvenv.cfg"),
                    "tree_sha256": "4" * 64,
                    "tree_entry_count": 2,
                    "tree_total_bytes": 32,
                    "base_root": str(hermes_base_python.parent.parent),
                    "base_python_path": str(hermes_base_python),
                    "base_python_sha256": base_python_sha,
                    "base_tree_sha256": base_python_environment["sha256"],
                    "base_tree_entry_count": base_python_environment["entry_count"],
                    "base_tree_total_bytes": base_python_environment["total_bytes"],
                    "sha256": "5" * 64,
                },
                "launcher": {
                    "path": str(module.HERMES_J4_LAUNCHER.resolve()),
                    "sha256": module._sha256_file(module.HERMES_J4_LAUNCHER.resolve()),
                },
                "source": {
                    "root": str(hermes_source),
                    "origin_root": str(hermes_source),
                    "sha256": "6" * 64,
                    "revision": "hermes-revision",
                    "clean": True,
                    "lock_sha256": "7" * 64,
                    "excluded_runtime_roots": [],
                    "scope": module.HERMES_J4_SOURCE_SCOPE,
                    "frozen": True,
                },
                "auth_profile": auth_profile,
            },
        },
        hermes_python=hermes_base_python,
        hermes_venv_python=hermes_venv_python,
        hermes_base_python_root=hermes_base_python.parent.parent,
        hermes_launcher=module.HERMES_J4_LAUNCHER.resolve(),
        hermes_source_root=hermes_source,
        hermes_source_paths=frozenset(),
        hermes_site_packages=site_packages,
        hermes_auth_projection=auth_projection,
        hermes_auth_profile=auth_profile,
        hermes_auth_source=auth_source,
        hermes_auth_source_sha256=module._sha256_file(auth_source),
        hermes_auth_projection_sha256=module._sha256_json(auth_projection),
        hermes_auth_run_nonce=auth_run_nonce,
    )


def test_freecode_attestation_binds_exact_bare_selected_model_and_rejects_other_models() -> None:
    import app.evals.bakeoff_runtime as runtime

    invocation_env = {"CLAUDE_CODE_USE_OPENAI": "1"}
    namespaced = runtime._freecode_attestation(
        {"modelUsage": {"openai/gpt-5.4": {"inputTokens": 10}}, "num_turns": 1},
        invocation_env=invocation_env,
    )
    bare = runtime._freecode_attestation(
        {"modelUsage": {"gpt-5.4": {"inputTokens": 10}}, "num_turns": 1},
        invocation_env=invocation_env,
    )
    unrelated = runtime._freecode_attestation(
        {"modelUsage": {"gpt-5.3": {"inputTokens": 10}}, "num_turns": 1},
        invocation_env=invocation_env,
    )
    mixed = runtime._freecode_attestation(
        {
            "modelUsage": {
                "gpt-5.4": {"inputTokens": 10},
                "gpt-5.3": {"inputTokens": 10},
            },
            "num_turns": 2,
        },
        invocation_env=invocation_env,
    )
    missing_route_env = runtime._freecode_attestation({"modelUsage": {"gpt-5.4": {"inputTokens": 10}}, "num_turns": 1})

    assert namespaced[:3] == ("gpt-5.4", "chatgpt-codex", False)
    assert bare[:3] == ("gpt-5.4", "chatgpt-codex", False)
    assert unrelated[:3] == ("gpt-5.3", "chatgpt-codex", True)
    assert mixed[:3] == (None, "chatgpt-codex", True)
    assert missing_route_env[:3] == ("gpt-5.4", None, True)


def test_hermes_attestation_rejects_any_positive_unattributed_call_segment() -> None:
    import app.evals.bakeoff_runtime as runtime

    result = runtime._hermes_attestation(
        [
            {
                "model": "openai/gpt-5.4",
                "billing_provider": "openai-codex",
                "api_call_count": 1,
                "input_tokens": 10,
            },
            {"api_call_count": 1, "input_tokens": 5},
        ]
    )

    assert result[-1] is False

    valid_segment = {
        "model": "openai/gpt-5.4",
        "billing_provider": "openai-codex",
        "api_call_count": 1,
        "input_tokens": 10,
    }
    duplicate = runtime._hermes_attestation([valid_segment, dict(valid_segment)])
    assert duplicate[-1] is False


def test_freecode_j4_adapter_uses_exact_stdout_and_explicit_profile_env(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    calls: list[dict[str, object]] = []
    model_usage_key = ["openai/gpt-5.4"]
    freecode_binary = Path(prepared.freecode_manifest["artifact"]["path"])
    freecode_binary_sha = runtime._sha256_file(freecode_binary)
    binary = {
        "path": str(freecode_binary),
        "version": "free-code 1.0",
        "sha256": freecode_binary_sha,
        "revision": "freecode-revision",
        "runtime_sha256": "8" * 64,
        "components": {
            "build_manifest": {
                "sha256": prepared.freecode_manifest_sha256,
                "schema": runtime.FREECODE_BUILD_MANIFEST_SCHEMA,
            },
            "source": dict(prepared.freecode_manifest["source"]),
            "authority_guard": {
                "path": str(prepared.freecode_hook),
                "sha256": prepared.freecode_hook_sha256,
                "python_path": str(prepared.freecode_hook_python),
                "python_sha256": prepared.freecode_hook_python_sha256,
                "python_environment_sha256": prepared.freecode_hook_python_environment_sha256,
            },
        },
    }
    monkeypatch.setattr(
        runtime,
        "_freeze_freecode_runtime",
        lambda _prepared, *, workspace_root: (binary, freecode_binary, prepared.freecode_hook, []),
    )
    monkeypatch.setattr(runtime, "_freecode_prepared_runtime_stable", lambda _prepared, _config: True)

    def fake_run(
        command,
        cwd,
        timeout_seconds,
        *,
        env_overrides=None,
        env_remove=(),
        require_workspace_sandbox=False,
        additional_writable_roots=(),
        network_access=True,
    ):
        calls.append(
            {
                "command": command,
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "env_overrides": env_overrides,
                "env_remove": env_remove,
                "require_workspace_sandbox": require_workspace_sandbox,
                "additional_writable_roots": additional_writable_roots,
                "network_access": network_access,
            }
        )
        (cwd / "memory_answer.md").write_text("cedar-lantern", encoding="utf-8")
        outer = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": _j4_success_payload(),
            "modelUsage": {model_usage_key[0]: {"inputTokens": 10, "outputTokens": 5}},
            "num_turns": 2,
            "total_cost_usd": 0.01,
        }
        return runtime.ProcessRunResult(
            command=command,
            cwd=str(cwd),
            returncode=0,
            stdout=json.dumps(outer),
            stderr="",
            duration_ms=9,
            sandbox={"status": "enforced", "provider": "sandbox-exec", "reason": "test"},
        )

    monkeypatch.setattr(runtime, "_run_process", fake_run)

    receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "completed"
    assert receipt["effective_model"] == "gpt-5.4"
    assert receipt["effective_provider"] == "chatgpt-codex"
    assert receipt["fallback_observed"] is False
    assert receipt["tokens"] == 15
    assert receipt["score"] is None
    workspace_root = runtime._runtime_workspace_path(tmp_path, "freecode", envelope["envelope_id"])
    state_root = workspace_root.parent / "state"
    temporary = state_root / "tmp"
    assert calls[0]["env_overrides"] == {
        "CLAUDE_CODE_USE_OPENAI": "1",
        "HIVE_J4_WORKSPACE_ROOT": str(workspace_root.resolve()),
        "HIVE_J4_FREECODE_HOOK_LOG": str((state_root / "hook.jsonl").resolve()),
        "TMPDIR": str(temporary.resolve()),
        "TMP": str(temporary.resolve()),
        "TEMP": str(temporary.resolve()),
    }
    assert calls[0]["command"] == receipt["argv"]
    assert calls[0]["require_workspace_sandbox"] is True
    assert calls[0]["additional_writable_roots"] == (state_root,)
    assert receipt["route_attestation"]["call_count"] == 1
    assert receipt["route_attestation"]["count_semantics"] == "minimum_observed"
    assert receipt["route_attestation"]["provider_binding"] == {
        "route": "chatgpt-codex",
        "invocation_env": {"name": "CLAUDE_CODE_USE_OPENAI", "value": "1"},
        "source_contract": runtime.FREECODE_CODEX_PROVIDER_CONTRACT,
        "attested": True,
    }
    assert runtime.FREECODE_CODEX_PROVIDER_CONTRACT in receipt["attestation_source"]
    assert receipt["authority"]["sandbox"]["status"] == "enforced"
    assert receipt["resources"]["effective"] == receipt["resources"]["requested"]
    assert isinstance(receipt["argv"], list)
    runtime_root = workspace_root.parent / "runtime"
    assert receipt["execution"]["attempt_roots_owned"] is True
    assert receipt["execution"]["state_cleanup_verified"] is True
    assert not runtime_root.exists()
    assert not state_root.exists()

    model_usage_key[0] = "gpt-5.4"
    bare_provider_receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path / "bare-provider",
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )
    assert bare_provider_receipt["status"] == "completed"
    assert bare_provider_receipt["effective_provider"] == "chatgpt-codex"

    model_usage_key[0] = "gpt-5.3"
    unrelated_model_receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path / "unrelated-model",
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )
    assert unrelated_model_receipt["status"] == "attestation_failed"
    assert unrelated_model_receipt["effective_model"] == "gpt-5.3"
    assert unrelated_model_receipt["fallback_observed"] is True


def test_freecode_wrapper_cleans_owned_roots_after_completed_and_failed_receipts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    statuses = iter(("completed", "failed"))

    def fake_attempt(*_args, output_dir: Path, **_kwargs):
        workspace_root = runtime._runtime_workspace_path(output_dir, "freecode", envelope["envelope_id"])
        runtime_root = workspace_root.parent / "runtime"
        state_root = workspace_root.parent / "state"
        assert runtime_root.is_dir()
        assert state_root.is_dir()
        (runtime_root / "owned-runtime-marker").write_text("owned", encoding="utf-8")
        (state_root / "owned-state-marker").write_text("owned", encoding="utf-8")
        status = next(statuses)
        return {
            "status": status,
            "execution": {},
            "parsed": {"schema_valid": status == "completed", "schema_errors": []},
        }

    monkeypatch.setattr(runtime, "_run_freecode_j4_attempt", fake_attempt)

    for status in ("completed", "failed"):
        output_dir = tmp_path / status
        receipt = runtime._run_freecode_j4(
            scenario,
            envelope,
            envelope_sha256,
            output_dir=output_dir,
            config=runtime.J4RuntimeConfig(external_profile_authorized=True),
            prepared=prepared,
        )
        attempt_root = runtime._runtime_workspace_path(
            output_dir,
            "freecode",
            envelope["envelope_id"],
        ).parent
        assert receipt["status"] == status
        assert receipt["execution"] == {
            "attempt_roots_owned": True,
            "state_cleanup_verified": True,
        }
        assert not (attempt_root / "runtime").exists()
        assert not (attempt_root / "state").exists()


def test_freecode_wrapper_cleans_owned_roots_when_attempt_raises(monkeypatch, tmp_path: Path) -> None:
    import pytest

    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    workspace_root = runtime._runtime_workspace_path(tmp_path, "freecode", envelope["envelope_id"])
    attempt_root = workspace_root.parent

    def exploding_attempt(*_args, **_kwargs):
        (attempt_root / "runtime" / "owned-runtime-marker").write_text("owned", encoding="utf-8")
        (attempt_root / "state" / "owned-state-marker").write_text("owned", encoding="utf-8")
        raise RuntimeError("synthetic adapter failure")

    monkeypatch.setattr(runtime, "_run_freecode_j4_attempt", exploding_attempt)

    with pytest.raises(RuntimeError, match="synthetic adapter failure"):
        runtime._run_freecode_j4(
            scenario,
            envelope,
            envelope_sha256,
            output_dir=tmp_path,
            config=runtime.J4RuntimeConfig(external_profile_authorized=True),
            prepared=prepared,
        )

    assert not (attempt_root / "runtime").exists()
    assert not (attempt_root / "state").exists()


def test_freecode_wrapper_preserves_unowned_conflicting_state_root(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    workspace_root = runtime._runtime_workspace_path(tmp_path, "freecode", envelope["envelope_id"])
    attempt_root = workspace_root.parent
    state_root = attempt_root / "state"
    state_root.mkdir(parents=True)
    sentinel = state_root / "unowned-sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(
        runtime,
        "_run_freecode_j4_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("conflict must not execute")),
    )

    receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "needs_reconciliation"
    assert receipt["parsed"]["schema_errors"] == ["freecode_attempt_state_conflict"]
    assert receipt["execution"] == {
        "attempt_roots_owned": False,
        "state_cleanup_verified": False,
    }
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not (attempt_root / "runtime").exists()


def test_freecode_wrapper_rejects_parent_symlink_without_touching_foreign_tree(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    output_dir = tmp_path / "symlinked-output"
    output_dir.mkdir()
    foreign_root = tmp_path / "foreign-tree"
    foreign_root.mkdir()
    sentinel = foreign_root / "foreign-sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    (output_dir / "j4_runtime").symlink_to(foreign_root, target_is_directory=True)
    monkeypatch.setattr(
        runtime,
        "_run_freecode_j4_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("symlinked parent must not execute")),
    )

    receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=output_dir,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "needs_reconciliation"
    assert receipt["parsed"]["schema_errors"] == ["freecode_attempt_boundary_unsupported"]
    assert receipt["execution"] == {
        "attempt_roots_owned": False,
        "state_cleanup_verified": False,
    }
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not (foreign_root / "freecode").exists()
    assert (output_dir / "j4_runtime").is_symlink()


def test_freecode_wrapper_creates_missing_output_parents_for_normal_setup(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    output_dir = tmp_path / "deep" / "nested" / "output"

    def fake_attempt(*_args, output_dir: Path, **_kwargs):
        workspace_root = runtime._runtime_workspace_path(output_dir, "freecode", envelope["envelope_id"])
        assert (workspace_root.parent / "runtime").is_dir()
        assert (workspace_root.parent / "state").is_dir()
        return {
            "status": "completed",
            "execution": {},
            "parsed": {"schema_valid": True, "schema_errors": []},
        }

    monkeypatch.setattr(runtime, "_run_freecode_j4_attempt", fake_attempt)

    receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=output_dir,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "completed"
    assert receipt["execution"] == {
        "attempt_roots_owned": True,
        "state_cleanup_verified": True,
    }
    assert output_dir.is_dir()


def test_freecode_wrapper_reports_replaced_attempt_as_typed_reconciliation(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    output_dir = tmp_path / "replaced"
    workspace_root = runtime._runtime_workspace_path(output_dir, "freecode", envelope["envelope_id"])
    attempt_root = workspace_root.parent

    def replacing_attempt(*_args, **_kwargs):
        stolen = tmp_path / "stolen-attempt"
        attempt_root.rename(stolen)
        attempt_root.mkdir(mode=0o700)
        (attempt_root / "foreign-sentinel").write_text("preserve", encoding="utf-8")
        return {
            "status": "completed",
            "execution": {},
            "parsed": {"schema_valid": True, "schema_errors": []},
        }

    monkeypatch.setattr(runtime, "_run_freecode_j4_attempt", replacing_attempt)

    receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=output_dir,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "needs_reconciliation"
    assert receipt["execution"] == {
        "attempt_roots_owned": False,
        "state_cleanup_verified": False,
    }
    assert receipt["parsed"]["schema_errors"] == ["freecode_attempt_state_cleanup_ambiguous"]
    assert (attempt_root / "foreign-sentinel").read_text(encoding="utf-8") == "preserve"
    assert (tmp_path / "stolen-attempt" / "runtime").is_dir()
    assert (tmp_path / "stolen-attempt" / "state").is_dir()


def test_freecode_wrapper_does_not_verify_cleanup_behind_dangling_symlink(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    output_dir = tmp_path / "dangling"
    workspace_root = runtime._runtime_workspace_path(output_dir, "freecode", envelope["envelope_id"])
    attempt_root = workspace_root.parent
    real_rmtree = shutil.rmtree

    def rmtree_then_dangling_link(path, *args, **kwargs):
        real_rmtree(path, *args, **kwargs)
        state_root = attempt_root / "state"
        if path == state_root and not state_root.exists():
            state_root.symlink_to(attempt_root / "missing-target", target_is_directory=True)

    monkeypatch.setattr(runtime.shutil, "rmtree", rmtree_then_dangling_link)
    monkeypatch.setattr(
        runtime,
        "_run_freecode_j4_attempt",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "execution": {},
            "parsed": {"schema_valid": True, "schema_errors": []},
        },
    )

    receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=output_dir,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "needs_reconciliation"
    assert receipt["execution"] == {
        "attempt_roots_owned": True,
        "state_cleanup_verified": False,
    }
    assert receipt["parsed"]["schema_errors"] == ["freecode_attempt_state_cleanup_failed"]
    assert (attempt_root / "state").is_symlink()


def test_freecode_wrapper_preserves_replaced_child_root_and_reports_reconciliation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    output_dir = tmp_path / "child-replaced"
    workspace_root = runtime._runtime_workspace_path(output_dir, "freecode", envelope["envelope_id"])
    attempt_root = workspace_root.parent
    foreign = tmp_path / "foreign-state-dir"
    foreign.mkdir()
    (foreign / "foreign-sentinel").write_text("preserve", encoding="utf-8")

    def replacing_child_dir(*_args, **_kwargs):
        # Same-UID replacement of a child the wrapper created: cleanup must
        # verify the child's exact identity, not delete it by path.
        state_root = attempt_root / "state"
        state_root.rmdir()
        shutil.move(str(foreign), str(state_root))
        return {
            "status": "completed",
            "execution": {},
            "parsed": {"schema_valid": True, "schema_errors": []},
        }

    monkeypatch.setattr(runtime, "_run_freecode_j4_attempt", replacing_child_dir)

    receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=output_dir,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "needs_reconciliation"
    assert receipt["execution"] == {
        "attempt_roots_owned": True,
        "state_cleanup_verified": False,
    }
    assert receipt["parsed"]["schema_errors"] == ["freecode_attempt_state_cleanup_ambiguous"]
    assert receipt["parsed"]["schema_valid"] is False
    assert (attempt_root / "state" / "foreign-sentinel").read_text(encoding="utf-8") == "preserve"
    assert not (attempt_root / "runtime").exists()


def test_freecode_partial_setup_rollback_preserves_replaced_runtime_root(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    output_dir = tmp_path / "partial-setup"
    workspace_root = runtime._runtime_workspace_path(output_dir, "freecode", envelope["envelope_id"])
    attempt_root = workspace_root.parent
    foreign = tmp_path / "foreign-runtime-dir"
    foreign.mkdir()
    (foreign / "synthetic-sentinel").write_text("must survive", encoding="utf-8")
    original_mkdir = Path.mkdir

    def fail_state_after_runtime_replaced(path, *args, **kwargs):
        if path == attempt_root / "state":
            (attempt_root / "runtime").rename(tmp_path / "preserved-owned-runtime")
            foreign.rename(attempt_root / "runtime")
            raise FileExistsError("synthetic second-child conflict")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_state_after_runtime_replaced)
    monkeypatch.setattr(
        runtime,
        "_run_freecode_j4_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("setup failure must not execute the adapter")),
    )

    receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=output_dir,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "needs_reconciliation"
    assert receipt["parsed"]["schema_errors"] == [
        "freecode_attempt_state_cleanup_ambiguous",
        "freecode_attempt_state_conflict",
    ]
    assert receipt["parsed"]["schema_valid"] is False
    assert receipt["execution"]["attempt_roots_owned"] is True
    assert receipt["execution"]["state_cleanup_verified"] is False
    assert (attempt_root / "runtime" / "synthetic-sentinel").read_text(encoding="utf-8") == "must survive"
    assert (tmp_path / "preserved-owned-runtime").is_dir()


def test_blocked_adapters_do_not_traverse_or_write_unclaimed_workspace(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)

    def must_not_traverse(*_args, **_kwargs):
        raise AssertionError("an unclaimed workspace must not be traversed, read or hashed")

    monkeypatch.setattr(runtime, "_workspace_receipt", must_not_traverse)
    monkeypatch.setattr(
        runtime,
        "_run_freecode_j4_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("blocked adapter must not execute")),
    )
    monkeypatch.setattr(
        runtime,
        "_run_hermes_j4_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("blocked adapter must not execute")),
    )

    adapters = {
        "freecode": runtime._run_freecode_j4,
        "hermes": runtime._run_hermes_j4,
        "hive": runtime._run_hive_j4,
    }
    for runtime_name, adapter in adapters.items():
        output_dir = tmp_path / f"out-{runtime_name}"
        output_dir.mkdir()
        foreign_root = tmp_path / f"foreign-{runtime_name}"
        foreign_workspace = foreign_root / runtime_name / envelope["envelope_id"] / "workspace"
        foreign_workspace.mkdir(parents=True)
        sentinel = foreign_workspace / "synthetic-private-marker.txt"
        sentinel.write_text("test-owned synthetic data only", encoding="utf-8")
        (output_dir / "j4_runtime").symlink_to(foreign_root, target_is_directory=True)

        kwargs: dict = {"output_dir": output_dir, "config": runtime.J4RuntimeConfig()}
        if runtime_name != "hive":
            kwargs["prepared"] = prepared
        receipt = adapter(scenario, envelope, envelope_sha256, **kwargs)

        assert receipt["status"] == "needs_reconciliation"
        assert receipt["parsed"]["schema_errors"] == [f"{runtime_name}_attempt_boundary_unsupported"]
        workspace = receipt["workspace"]
        assert workspace["before_manifest"] is None
        assert workspace["before_sha256"] is None
        assert workspace["after_manifest"] is None
        assert workspace["after_sha256"] is None
        assert workspace["diff"] is None
        assert workspace["boundary_ok"] is None
        assert workspace["boundary_errors"] == ["workspace_unobserved:unclaimed_attempt_root"]
        assert workspace["file_count"] is None
        assert workspace["total_bytes"] is None
        assert workspace["root_identity"] is None
        if runtime_name == "hermes":
            assert receipt["execution"]["state_root_owned"] is False
        elif runtime_name == "freecode":
            assert receipt["execution"]["attempt_roots_owned"] is False
        else:
            assert receipt["execution"]["terminal_status"] is None
        assert sentinel.read_text(encoding="utf-8") == "test-owned synthetic data only"
        assert list(foreign_workspace.iterdir()) == [sentinel]
        assert (output_dir / "j4_runtime").is_symlink()


def test_replaced_output_sibling_trees_are_never_written_through(monkeypatch, tmp_path: Path) -> None:
    """j4_artifacts / j4_scoring are claimed before any byte lands in them.

    A pre-planted symlink at any sibling-output level must produce a typed
    failure with no foreign write, no foreign path published, and no scoring
    snapshot — the same ownership contract the attempt root already has.
    """
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())

    # B1 shape: output/j4_artifacts symlinked into a foreign tree; the hive
    # adapter's typed setup-error path must not write artifacts through it.
    hive_output = tmp_path / "hive-output"
    hive_output.mkdir()
    foreign_artifacts = tmp_path / "foreign-artifacts"
    foreign_artifacts.mkdir()
    (foreign_artifacts / "pre-existing.txt").write_text("foreign bytes", encoding="utf-8")
    (hive_output / "j4_artifacts").symlink_to(foreign_artifacts, target_is_directory=True)
    hive_receipt = runtime._run_hive_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=hive_output,
        config=runtime.J4RuntimeConfig(),
    )
    assert hive_receipt["status"] == "resource_unavailable"
    assert hive_receipt["artifacts"] == {}
    assert "j4_artifacts_boundary_unsupported" in hive_receipt["parsed"]["schema_errors"]
    assert [path.name for path in foreign_artifacts.iterdir()] == ["pre-existing.txt"]
    published = json.dumps(hive_receipt)
    assert "foreign-artifacts" not in published

    # A helper-level conflict form: a real pre-existing artifacts leaf is a
    # typed boundary failure too, never merged into or overwritten.
    conflict_output = tmp_path / "conflict-output"
    conflict_output.mkdir()
    conflict_leaf = conflict_output / "j4_artifacts" / "freecode" / str(envelope["envelope_id"])
    conflict_leaf.mkdir(parents=True)
    (conflict_leaf / "foreign-keep.txt").write_text("foreign bytes", encoding="utf-8")
    conflict_artifacts, conflict_errors = runtime._write_j4_artifacts(
        conflict_output,
        runtime="freecode",
        envelope_id=str(envelope["envelope_id"]),
        stdout="x",
        stderr="y",
    )
    assert conflict_artifacts == {}
    assert conflict_errors == ["j4_artifacts_boundary_conflict"]
    assert [path.name for path in conflict_leaf.iterdir()] == ["foreign-keep.txt"]

    # B2 shape: output/j4_scoring symlinked into a foreign tree; a verified
    # completed receipt must never be copied into it.
    scoring_output = tmp_path / "scoring-output"
    scoring_output.mkdir()
    workspace_root = runtime._runtime_workspace_path(scoring_output, "freecode", envelope["envelope_id"])
    before, clone_errors, workspace_identity = runtime._clone_seed(scenario.workspace_dir, workspace_root)
    assert clone_errors == []
    completed_receipt = {
        "status": "completed",
        "workspace": runtime._workspace_receipt(
            workspace_root, before, envelope=envelope, workspace_identity=workspace_identity
        ),
    }
    foreign_scoring = tmp_path / "foreign-scoring"
    foreign_scoring.mkdir()
    (foreign_scoring / "pre-existing.txt").write_text("foreign bytes", encoding="utf-8")
    (scoring_output / "j4_scoring").symlink_to(foreign_scoring, target_is_directory=True)
    snapshot_path, snapshot_sha, snapshot_errors = runtime._create_scoring_snapshot(
        output_dir=scoring_output,
        runtime="freecode",
        envelope=envelope,
        receipt=completed_receipt,
    )
    assert snapshot_path is None and snapshot_sha == ""
    assert snapshot_errors == ["scoring_destination_unsupported"]
    assert [path.name for path in foreign_scoring.iterdir()] == ["pre-existing.txt"]

    # The scorer freeze is a second j4_scoring writer and must refuse too.
    freeze_output = tmp_path / "freeze-output"
    freeze_output.mkdir()
    freeze_foreign = tmp_path / "foreign-freeze"
    freeze_foreign.mkdir()
    (freeze_output / "j4_scoring").symlink_to(freeze_foreign, target_is_directory=True)
    frozen, freeze_errors = runtime._freeze_scorer_source(freeze_output, runtime._scorer_runtime_identity())
    assert frozen is None
    assert freeze_errors == ["scorer_boundary_unsupported"]
    assert list(freeze_foreign.iterdir()) == []

    # Ordinary valid destinations still work: same helper, untouched tree.
    clean_output = tmp_path / "clean-output"
    clean_output.mkdir()
    clean_workspace = runtime._runtime_workspace_path(clean_output, "freecode", envelope["envelope_id"])
    clean_before, clean_clone_errors, clean_identity = runtime._clone_seed(scenario.workspace_dir, clean_workspace)
    assert clean_clone_errors == []
    clean_receipt = {
        "status": "completed",
        "workspace": runtime._workspace_receipt(
            clean_workspace, clean_before, envelope=envelope, workspace_identity=clean_identity
        ),
    }
    clean_path, clean_sha, clean_errors = runtime._create_scoring_snapshot(
        output_dir=clean_output,
        runtime="freecode",
        envelope=envelope,
        receipt=clean_receipt,
    )
    assert clean_errors == [] and clean_path is not None and len(clean_sha) == 64
    assert clean_path == clean_output / "j4_scoring" / "freecode" / str(envelope["envelope_id"]) / "workspace"
    assert sorted(path.name for path in clean_path.iterdir()) == sorted(path.name for path in clean_workspace.iterdir())


def _freecode_positive_patches(monkeypatch, module, prepared, tmp_path: Path) -> None:
    """Apply the minimal fake freeze/process patches of the positive test."""
    freecode_binary = Path(prepared.freecode_manifest["artifact"]["path"])
    binary = {
        "path": str(freecode_binary),
        "version": "free-code 1.0",
        "sha256": module._sha256_file(freecode_binary),
        "revision": "freecode-revision",
        "runtime_sha256": "8" * 64,
        "components": {
            "build_manifest": {
                "sha256": prepared.freecode_manifest_sha256,
                "schema": module.FREECODE_BUILD_MANIFEST_SCHEMA,
            },
            "source": dict(prepared.freecode_manifest["source"]),
            "authority_guard": {
                "path": str(prepared.freecode_hook),
                "sha256": prepared.freecode_hook_sha256,
                "python_path": str(prepared.freecode_hook_python),
                "python_sha256": prepared.freecode_hook_python_sha256,
                "python_environment_sha256": prepared.freecode_hook_python_environment_sha256,
            },
        },
    }
    monkeypatch.setattr(
        module,
        "_freeze_freecode_runtime",
        lambda _prepared, *, workspace_root: (binary, freecode_binary, prepared.freecode_hook, []),
    )
    monkeypatch.setattr(module, "_freecode_prepared_runtime_stable", lambda _prepared, _config: True)


def _freecode_success_result(module, command, cwd):
    outer = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": _j4_success_payload(),
        "modelUsage": {"openai/gpt-5.4": {"inputTokens": 10, "outputTokens": 5}},
        "num_turns": 2,
        "total_cost_usd": 0.01,
    }
    return module.ProcessRunResult(
        command=command,
        cwd=str(cwd),
        returncode=0,
        stdout=json.dumps(outer),
        stderr="",
        duration_ms=9,
        sandbox={"status": "enforced", "provider": "sandbox-exec", "reason": "test"},
    )


@pytest.mark.parametrize("replacement", ["real_directory", "ancestor_symlink"])
def test_replaced_workspace_is_unobserved_and_never_read_or_scored(
    monkeypatch, tmp_path: Path, replacement: str
) -> None:
    """A workspace swapped mid-attempt is foreign evidence, not a score source.

    The real adapter runs with the real clone; a same-UID writer replaces the
    workspace with a different real directory (or swaps an ancestor symlink)
    while the process result is produced. The receipt must record the
    workspace as unobserved — never traverse, hash or publish the foreign
    bytes — and the scoring snapshot must refuse without reading.
    """
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    _freecode_positive_patches(monkeypatch, runtime, prepared, tmp_path)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    foreign_root = tmp_path / "foreign"
    foreign_workspace = foreign_root / "freecode" / str(envelope["envelope_id"]) / "workspace"
    foreign_workspace.mkdir(parents=True)
    (foreign_workspace / "synthetic-private-marker.txt").write_text("synthetic only", encoding="utf-8")

    def swap_after_process(command, cwd, *args, **kwargs):
        result = _freecode_success_result(runtime, command, cwd)
        workspace = Path(cwd)
        if replacement == "ancestor_symlink":
            shutil.rmtree(output_dir / "j4_runtime")
            (output_dir / "j4_runtime").symlink_to(foreign_root, target_is_directory=True)
        else:
            workspace.rename(workspace.with_name("owned-workspace-retained"))
            workspace.mkdir()
            (workspace / "synthetic-foreign-marker.txt").write_text("synthetic foreign bytes", encoding="utf-8")
        return result

    monkeypatch.setattr(runtime, "_run_process", swap_after_process)

    receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=output_dir,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )
    snapshot_path, snapshot_sha, snapshot_errors = runtime._create_scoring_snapshot(
        output_dir=output_dir,
        runtime="freecode",
        envelope=envelope,
        receipt=receipt,
    )

    workspace = receipt["workspace"]
    assert workspace["after_manifest"] is None
    assert workspace["after_sha256"] is None
    assert workspace["diff"] is None
    assert workspace["boundary_ok"] is None
    assert workspace["root_identity"] is None
    assert workspace["boundary_errors"] == ["workspace_unobserved:workspace_root_replaced"]
    assert receipt["status"] != "completed"
    assert "synthetic-private-marker" not in json.dumps(receipt)
    assert "synthetic-foreign-marker" not in json.dumps(receipt)
    assert snapshot_path is None and snapshot_sha == ""
    assert snapshot_errors == ["scoring_workspace_unverified"]
    assert (foreign_workspace / "synthetic-private-marker.txt").read_text(encoding="utf-8") == "synthetic only"
    if replacement == "real_directory":
        retained = output_dir / "j4_runtime" / "freecode" / str(envelope["envelope_id"]) / "owned-workspace-retained"
        assert (retained / "TASK.md").is_file()
    else:
        assert (output_dir / "j4_runtime").is_symlink()


def test_scoring_refuses_identical_content_at_a_replaced_inode(monkeypatch, tmp_path: Path) -> None:
    """Equal content at a different inode is not ownership proof.

    After a verified completed receipt, copying the workspace aside and
    moving the copy back yields byte-identical content at a new (st_dev,
    st_ino); the scoring boundary must still refuse by identity, not hash.
    """
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, _ = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    workspace_root = runtime._runtime_workspace_path(output_dir, "freecode", envelope["envelope_id"])
    before, clone_errors, workspace_identity = runtime._clone_seed(scenario.workspace_dir, workspace_root)
    assert clone_errors == []
    receipt = {
        "status": "completed",
        "workspace": runtime._workspace_receipt(
            workspace_root, before, envelope=envelope, workspace_identity=workspace_identity
        ),
    }
    assert receipt["workspace"]["boundary_ok"] is True

    aside = tmp_path / "aside"
    shutil.copytree(workspace_root, aside)
    shutil.rmtree(workspace_root)
    aside.rename(workspace_root)

    snapshot_path, snapshot_sha, snapshot_errors = runtime._create_scoring_snapshot(
        output_dir=output_dir,
        runtime="freecode",
        envelope=envelope,
        receipt=receipt,
    )
    assert snapshot_path is None and snapshot_sha == ""
    assert snapshot_errors == ["scoring_source_replaced"]
    assert not (output_dir / "j4_scoring").exists()


def test_hermes_j4_adapter_exports_only_anchored_session_and_attests_route(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    calls: list[dict[str, object]] = []
    include_unattributed_call = [False]
    monkeypatch.setattr(runtime, "_hermes_frozen_runtime_stable", lambda _prepared: True)

    def fake_run(
        command,
        cwd,
        timeout_seconds,
        *,
        env_overrides=None,
        env_remove=(),
        require_workspace_sandbox=False,
        additional_writable_roots=(),
        network_access=True,
    ):
        del timeout_seconds
        assert require_workspace_sandbox is True
        calls.append(
            {
                "command": command,
                "cwd": cwd,
                "env_overrides": env_overrides,
                "env_remove": env_remove,
                "additional_writable_roots": additional_writable_roots,
                "network_access": network_access,
            }
        )
        if "attest-session" in command:
            return runtime.ProcessRunResult(
                command,
                str(cwd),
                0,
                json.dumps(
                    {
                        "ok": True,
                        "session_id": "sess-1",
                        "tip_session_id": "sess-1",
                        "lineage_session_ids": ["sess-1"],
                        "session_count": 1,
                        "lineage_count": 1,
                        "source": "p08-j4",
                        "all_sources_match": True,
                        "prompt_sha256_match": True,
                        "tip_id_match": True,
                        "unique_lineage": True,
                        "errors": [],
                    }
                ),
                "",
                2,
                sandbox={"status": "enforced", "provider": "sandbox-exec", "reason": "test"},
            )
        if "sessions" in command and "export" in command:
            valid_segment = {
                "model": "openai/gpt-5.4",
                "billing_provider": "openai-codex",
                "model_config": {"provider": "openai-codex", "model": "gpt-5.4"},
                "api_call_count": 2,
                "input_tokens": 12,
                "output_tokens": 4,
                "estimated_cost_usd": 0.02,
            }
            transcript = {
                "id": "sess-1",
                "source": "p08-j4",
                "segments": (
                    [valid_segment, {"api_call_count": 1, "input_tokens": 5}]
                    if include_unattributed_call[0]
                    else [valid_segment]
                ),
            }
            return runtime.ProcessRunResult(
                command,
                str(cwd),
                0,
                json.dumps(transcript) + "\n",
                "",
                3,
                sandbox={"status": "enforced", "provider": "sandbox-exec", "reason": "test"},
            )
        (cwd / "memory_answer.md").write_text("cedar-lantern", encoding="utf-8")
        return runtime.ProcessRunResult(
            command,
            str(cwd),
            0,
            json.dumps(_j4_success_payload()),
            "session_id: sess-1\n",
            8,
            sandbox={"status": "enforced", "provider": "sandbox-exec", "reason": "test"},
        )

    monkeypatch.setattr(runtime, "_run_process", fake_run)

    receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "completed"
    assert receipt["effective_model"] == "gpt-5.4"
    assert receipt["effective_provider"] == "openai-codex"
    assert receipt["fallback_observed"] is False
    assert receipt["tokens"] == 16
    assert receipt["route_attestation"]["call_count"] == 2
    assert receipt["authority"]["effective"]["allowed_tools"] == [
        "read_file",
        "write_file",
        "patch",
        "search_files",
    ]
    assert calls[1]["command"] == [
        str(prepared.hermes_python),
        "-I",
        "-S",
        str(prepared.hermes_launcher),
        "attest-session",
        "--session-id",
        "sess-1",
        "--expected-prompt-sha256",
        envelope["task"]["prompt_sha256"],
        "--expected-source",
        "p08-j4",
    ]
    assert calls[2]["command"] == [
        str(prepared.hermes_python),
        "-I",
        "-S",
        str(prepared.hermes_launcher),
        "sessions",
        "export",
        "-",
        "--format",
        "jsonl",
        "--session-id",
        "sess-1",
        "--redact",
    ]
    assert receipt["argv"] == calls[0]["command"]
    state_root = (tmp_path / "j4_runtime" / "hermes" / envelope["envelope_id"] / "state").resolve()
    workspace_root = runtime._runtime_workspace_path(tmp_path, "hermes", envelope["envelope_id"])
    expected_env = {
        runtime.HERMES_J4_SOURCE_ROOT_ENV: str(prepared.hermes_source_root),
        runtime.HERMES_J4_STATE_DB_ENV: str(state_root / "state.db"),
        runtime.HERMES_J4_SITE_PACKAGES_ENV: str(prepared.hermes_site_packages),
        runtime.HERMES_J4_WORKSPACE_ROOT_ENV: str(workspace_root.resolve()),
        "HERMES_WRITE_SAFE_ROOT": str(workspace_root.resolve()),
        "PYTHONDONTWRITEBYTECODE": "1",
        **_expected_hermes_attempt_environment(state_root),
    }
    assert calls[0]["env_overrides"] == expected_env
    assert calls[1]["env_overrides"] == expected_env
    assert calls[2]["env_overrides"] == expected_env
    assert all(call["env_remove"] == runtime._HERMES_J4_AMBIENT_ENV_DENYLIST for call in calls)
    assert calls[0]["additional_writable_roots"] == (state_root,)
    assert calls[1]["additional_writable_roots"] == (state_root,)
    assert calls[2]["additional_writable_roots"] == (state_root,)
    assert calls[1]["network_access"] is False
    assert receipt["binary"]["runtime_sha256"] == "3" * 64
    assert not state_root.exists()
    assert _FAKE_HERMES_ACCESS_TOKEN not in json.dumps(receipt)

    include_unattributed_call[0] = True
    unattributed_receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path / "unattributed-call",
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )
    assert unattributed_receipt["status"] == "attestation_failed"


def test_hermes_j4_adapter_cleans_attempt_auth_after_process_failure(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    monkeypatch.setattr(runtime, "_hermes_frozen_runtime_stable", lambda _prepared: True)
    monkeypatch.setattr(
        runtime,
        "_run_process",
        lambda command, cwd, *_args, **_kwargs: runtime.ProcessRunResult(
            command=command,
            cwd=str(cwd),
            returncode=1,
            stdout="",
            stderr="provider request failed",
            duration_ms=3,
            sandbox={"status": "enforced", "provider": "test", "reason": "test"},
        ),
    )

    receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    auth_path = tmp_path / "j4_runtime" / "hermes" / envelope["envelope_id"] / "state" / "hermes-home" / "auth.json"
    assert receipt["status"] == "failed"
    assert not auth_path.exists()
    assert _FAKE_HERMES_ACCESS_TOKEN not in json.dumps(receipt)


def test_hermes_j4_adapter_cleans_partial_auth_after_home_initialization_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    created_auth: list[Path] = []

    def fail_after_auth_write(state_root: Path, _prepared) -> tuple[dict[str, str], list[str]]:
        auth_path = state_root / "hermes-home" / "auth.json"
        auth_path.parent.mkdir()
        auth_path.write_text(_FAKE_HERMES_ACCESS_TOKEN, encoding="utf-8")
        created_auth.append(auth_path)
        return {}, ["hermes_attempt_home_initialization_failed"]

    monkeypatch.setattr(runtime, "_initialize_hermes_attempt_home", fail_after_auth_write)
    monkeypatch.setattr(
        runtime,
        "_run_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("failed initialization must not execute")),
    )

    receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "resource_unavailable"
    assert len(created_auth) == 1
    assert not created_auth[0].exists()
    assert _FAKE_HERMES_ACCESS_TOKEN not in json.dumps(receipt)


def test_hermes_cleanup_error_is_recorded_in_parsed_receipt(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    cleaned_roots: list[Path] = []

    def fake_attempt(*_args, **_kwargs):
        return {
            "status": "completed",
            "execution": {},
            "parsed": {"schema_valid": True, "schema_errors": []},
        }

    def failed_cleanup(state_root: Path) -> list[str]:
        assert state_root.is_dir()
        cleaned_roots.append(state_root)
        return ["hermes_attempt_state_cleanup_failed"]

    monkeypatch.setattr(runtime, "_run_hermes_j4_attempt", fake_attempt)
    monkeypatch.setattr(runtime, "_cleanup_hermes_attempt_state", failed_cleanup)

    receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert len(cleaned_roots) == 1
    assert receipt["status"] == "needs_reconciliation"
    assert receipt["execution"] == {
        "state_root_owned": True,
        "state_cleanup_verified": False,
    }
    assert receipt["parsed"] == {
        "schema_valid": False,
        "schema_errors": ["hermes_attempt_state_cleanup_failed"],
    }


def test_hermes_wrapper_preserves_unowned_conflicting_attempt_root(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    output_dir = tmp_path / "hermes-conflict"
    output_dir.mkdir()
    attempt_root = output_dir / "j4_runtime" / "hermes" / envelope["envelope_id"]
    attempt_root.mkdir(parents=True)
    sentinel = attempt_root / "unowned-sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(
        runtime,
        "_run_hermes_j4_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("conflict must not execute")),
    )

    receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=output_dir,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "needs_reconciliation"
    assert receipt["parsed"]["schema_errors"] == ["hermes_attempt_state_conflict"]
    assert receipt["execution"] == {
        "state_root_owned": False,
        "state_cleanup_verified": False,
    }
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not (attempt_root / "workspace").exists()


def test_hermes_wrapper_rejects_parent_symlink_without_touching_foreign_tree(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    output_dir = tmp_path / "hermes-symlinked-output"
    output_dir.mkdir()
    foreign_root = tmp_path / "hermes-foreign-tree"
    foreign_root.mkdir()
    sentinel = foreign_root / "foreign-sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    (output_dir / "j4_runtime").symlink_to(foreign_root, target_is_directory=True)
    monkeypatch.setattr(
        runtime,
        "_run_hermes_j4_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("symlinked parent must not execute")),
    )

    receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=output_dir,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "needs_reconciliation"
    assert receipt["parsed"]["schema_errors"] == ["hermes_attempt_boundary_unsupported"]
    assert receipt["execution"] == {
        "state_root_owned": False,
        "state_cleanup_verified": False,
    }
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not (foreign_root / "hermes").exists()
    assert (output_dir / "j4_runtime").is_symlink()


def test_hermes_wrapper_reports_replaced_attempt_as_typed_reconciliation(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    output_dir = tmp_path / "hermes-replaced"
    workspace_root = runtime._runtime_workspace_path(output_dir, "hermes", envelope["envelope_id"])
    attempt_root = workspace_root.parent

    def replacing_attempt(*_args, **_kwargs):
        stolen = tmp_path / "hermes-stolen-attempt"
        attempt_root.rename(stolen)
        attempt_root.mkdir(mode=0o700)
        foreign_auth = attempt_root / "state" / "hermes-home" / "auth.json"
        foreign_auth.parent.mkdir(parents=True)
        foreign_auth.write_text("foreign-credentials-must-survive", encoding="utf-8")
        return {
            "status": "completed",
            "execution": {},
            "parsed": {"schema_valid": True, "schema_errors": []},
        }

    monkeypatch.setattr(runtime, "_run_hermes_j4_attempt", replacing_attempt)

    receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=output_dir,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "needs_reconciliation"
    assert receipt["execution"] == {
        "state_root_owned": False,
        "state_cleanup_verified": False,
    }
    assert receipt["parsed"]["schema_errors"] == ["hermes_attempt_state_cleanup_ambiguous"]
    foreign_auth = attempt_root / "state" / "hermes-home" / "auth.json"
    assert foreign_auth.read_text(encoding="utf-8") == "foreign-credentials-must-survive"
    assert (tmp_path / "hermes-stolen-attempt" / "state").is_dir()


def test_hermes_wrapper_does_not_verify_cleanup_behind_dangling_symlink(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    output_dir = tmp_path / "hermes-dangling"
    workspace_root = runtime._runtime_workspace_path(output_dir, "hermes", envelope["envelope_id"])
    attempt_root = workspace_root.parent
    state_root = attempt_root / "state"
    real_rmtree = shutil.rmtree

    def rmtree_then_dangling_link(path, *args, **kwargs):
        real_rmtree(path, *args, **kwargs)
        if path == state_root and not state_root.exists():
            state_root.symlink_to(attempt_root / "missing-target", target_is_directory=True)

    monkeypatch.setattr(runtime.shutil, "rmtree", rmtree_then_dangling_link)
    monkeypatch.setattr(
        runtime,
        "_run_hermes_j4_attempt",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "execution": {},
            "parsed": {"schema_valid": True, "schema_errors": []},
        },
    )

    receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=output_dir,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "needs_reconciliation"
    assert receipt["execution"] == {
        "state_root_owned": True,
        "state_cleanup_verified": False,
    }
    assert receipt["parsed"]["schema_errors"] == ["hermes_attempt_state_cleanup_failed"]
    assert state_root.is_symlink()


def test_hermes_wrapper_preserves_replaced_state_child_and_reports_reconciliation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    output_dir = tmp_path / "hermes-child-replaced"
    workspace_root = runtime._runtime_workspace_path(output_dir, "hermes", envelope["envelope_id"])
    attempt_root = workspace_root.parent
    foreign = tmp_path / "foreign-hermes-state"
    foreign.mkdir()
    (foreign / "foreign-sentinel").write_text("preserve", encoding="utf-8")

    def replacing_state_dir(*_args, **_kwargs):
        # Same-UID replacement of the state child the wrapper created: the
        # enclosing attempt root stays owned, so only exact child identity can
        # authorize the credential-state deletion.
        state_root = attempt_root / "state"
        state_root.rmdir()
        shutil.move(str(foreign), str(state_root))
        return {
            "status": "completed",
            "execution": {},
            "parsed": {"schema_valid": True, "schema_errors": []},
        }

    monkeypatch.setattr(runtime, "_run_hermes_j4_attempt", replacing_state_dir)

    receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=output_dir,
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert receipt["status"] == "needs_reconciliation"
    assert receipt["execution"] == {
        "state_root_owned": True,
        "state_cleanup_verified": False,
    }
    assert receipt["parsed"]["schema_errors"] == ["hermes_attempt_state_cleanup_ambiguous"]
    assert receipt["parsed"]["schema_valid"] is False
    assert (attempt_root / "state" / "foreign-sentinel").read_text(encoding="utf-8") == "preserve"


def test_hermes_attempt_attests_pre_state_db_from_direct_observation(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    monkeypatch.setattr(runtime, "_hermes_frozen_runtime_stable", lambda _prepared: True)
    monkeypatch.setattr(
        runtime,
        "_run_process",
        lambda command, cwd, *_args, **_kwargs: runtime.ProcessRunResult(
            command=command,
            cwd=str(cwd),
            returncode=1,
            stdout="",
            stderr="provider request failed",
            duration_ms=3,
            sandbox={"status": "enforced", "provider": "test", "reason": "test"},
        ),
    )
    original_clone = runtime._clone_seed

    def clone_and_plant_stale_state(seed_root: Path, workspace_root: Path):
        before, errors, workspace_identity = original_clone(seed_root, workspace_root)
        (workspace_root.parent / "state" / "state.db").write_bytes(b"stale-attempt-state")
        return before, errors, workspace_identity

    fresh_receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path / "fresh-state",
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    monkeypatch.setattr(runtime, "_clone_seed", clone_and_plant_stale_state)
    stale_receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path / "stale-state",
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
        prepared=prepared,
    )

    assert fresh_receipt["execution"]["pre_state_db_absent"] is True
    assert "state_not_fresh" not in fresh_receipt["parsed"]["schema_errors"]
    assert stale_receipt["execution"]["pre_state_db_absent"] is False
    assert "state_not_fresh" in stale_receipt["parsed"]["schema_errors"]
    assert stale_receipt["status"] != "completed"


def test_hive_j4_adapter_uses_public_session_http_and_redacts_bearer(monkeypatch, tmp_path: Path) -> None:
    from urllib.parse import urlparse

    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(runtime.uuid, "uuid4", lambda: SimpleNamespace(hex="attempt001"))

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self):
            self.files: dict[str, str] = {}
            self.requests: list[dict[str, object]] = []
            self.active_payload = None
            self.include_unattributed_call = False

        def request(self, method, url, **kwargs):
            parsed = urlparse(url)
            path = parsed.path
            params = kwargs.get("params") or {}
            payload = kwargs.get("json") or {}
            self.requests.append({"method": method, "path": path, **kwargs})
            if method == "GET" and path == "/api/health":
                return FakeResponse(
                    {
                        "version": "2026.08.31",
                        "components": {"build_identity": {"revision": "revision-1", "sha256": "c" * 64}},
                    }
                )
            if method == "GET" and path == "/api/v1/auth/me":
                return FakeResponse({"id": "user-1"})
            if method == "GET" and path == "/api/v1/agents/agent-1":
                return FakeResponse(
                    {
                        "primary_model_id": "model-1",
                        "fallback_model_id": None,
                        "max_tool_rounds": 6,
                    }
                )
            if method == "GET" and path == "/api/v1/enterprise/llm-models":
                return FakeResponse(
                    [
                        {
                            "id": "model-1",
                            "provider": "openai-response",
                            "model": "gpt-5.4",
                            "reasoning_effort": "low",
                            "enabled": True,
                        }
                    ]
                )
            if path == "/api/v1/agents/agent-1/files/content" and method == "PUT":
                self.files[str(params["path"])] = str(payload["content"])
                return FakeResponse({"status": "ok"})
            if path == "/api/v1/agents/agent-1/files/content" and method == "GET":
                return FakeResponse({"content": self.files[str(params["path"])]})
            if method == "POST" and path == "/api/v1/agents/agent-1/sessions":
                return FakeResponse(
                    {
                        "id": "session-1",
                        "permission_profile": {
                            "mode": payload.get("permission_mode"),
                            "allowed_tools": list(payload.get("allowed_tools") or []),
                            "writable_roots": list(payload.get("writable_roots") or []),
                            "readable_roots": list(payload.get("writable_roots") or []),
                            "capability_policy_snapshot": {"session_exact_scope": True},
                        },
                    },
                    status_code=201,
                )
            if method == "POST" and path == "/api/v1/agents/agent-1/sessions/session-1/runs":
                assert payload["model_routing_locked"] is True
                remote_root = next(name.rsplit("/", 1)[0] for name in self.files if name.endswith("/briefing.md"))
                self.files[f"{remote_root}/memory_answer.md"] = "cedar-lantern"
                return FakeResponse({"run_id": "run-1"}, status_code=201)
            if method == "GET" and path.endswith("/sessions/session-1/transcript"):
                final = json.dumps(_j4_success_payload())
                events = [
                    {
                        "scope": {"run_id": "run-1"},
                        "payload": {
                            "metadata": {
                                "event_type": "model_route",
                                "selected_model": "openai/gpt-5.4",
                                "selected_provider": "openai-response",
                                "model_routing_locked": True,
                                "fallback_model": None,
                            }
                        },
                    },
                    {
                        "scope": {"run_id": "run-1"},
                        "payload": {
                            "metadata": {
                                "event_type": "provider_call_ledger",
                                "provider_prompt_ledger": {
                                    "provider_call_id": "provider-call-1",
                                    "provider": "openai-response",
                                    "model": "gpt-5.4",
                                    "projected_input_tokens": 50,
                                    "tool_names": list(runtime._J4_ALLOWED_TOOLS["hive"]),
                                    "tool_schema_sha256": "d" * 64,
                                },
                                "cache_metrics": {"total_input_tokens": 48},
                                "tool_count": 5,
                            }
                        },
                    },
                    {
                        "kind": "assistant_text.completed",
                        "lifecycle": "completed",
                        "scope": {"run_id": "run-1"},
                        "payload": {"content": final},
                    },
                    {
                        "kind": "run.completed",
                        "lifecycle": "completed",
                        "scope": {"run_id": "run-1"},
                        "payload": {},
                    },
                ]
                if self.include_unattributed_call:
                    events.insert(
                        2,
                        {
                            "scope": {"run_id": "run-1"},
                            "payload": {
                                "metadata": {
                                    "event_type": "provider_call_ledger",
                                    "provider_prompt_ledger": {"provider_call_id": "provider-call-2"},
                                }
                            },
                        },
                    )
                return FakeResponse(events)
            if method == "GET" and path.endswith("/sessions/session-1/runs/active"):
                return FakeResponse(self.active_payload)
            if method == "GET" and path == "/api/v1/agents/agent-1/files/":
                root = str(params["path"])
                return FakeResponse(
                    [{"path": name, "is_dir": False} for name in sorted(self.files) if name.startswith(f"{root}/")]
                )
            raise AssertionError(f"unexpected request: {method} {path}")

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    client = FakeClient()
    config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="bearer-secret",
        hive_agent_id="agent-1",
        hive_revision="revision-1",
        hive_binary_sha256="c" * 64,
        external_profile_authorized=True,
        http_client=client,
    )
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=config)

    receipt = runtime._run_hive_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path,
        config=config,
    )

    assert receipt["status"] == "completed"
    assert receipt["effective_model"] == "gpt-5.4"
    assert receipt["effective_provider"] == "openai-response"
    assert receipt["fallback_observed"] is False
    assert receipt["workspace"]["boundary_ok"] is True
    assert receipt["route_attestation"]["call_count"] == 1
    assert receipt["route_attestation"]["tool_names"] == list(runtime._J4_ALLOWED_TOOLS["hive"])
    assert receipt["route_attestation"]["tool_schema_sha256"] == "d" * 64
    assert receipt["execution"]["attempt_id"] == "attempt001"
    assert receipt["execution"]["session_id"] == "session-1"
    assert receipt["execution"]["run_id"] == "run-1"
    assert receipt["execution"]["remote_root"].startswith("workspace/p08-j4/attempt001/")
    create_request = next(
        request
        for request in client.requests
        if request["method"] == "POST" and str(request["path"]).endswith("/sessions")
    )
    assert create_request["json"]["permission_mode"] == "bypassPermissions"
    assert create_request["json"]["allowed_tools"] == list(runtime._J4_ALLOWED_TOOLS["hive"])
    assert create_request["json"]["writable_roots"] == [receipt["execution"]["remote_root"]]
    assert any(request["method"] == "PUT" for request in client.requests)
    run_request = next(request for request in client.requests if str(request["path"]).endswith("/runs"))
    assert run_request["json"]["idempotency_key"].endswith(":attempt001")
    assert "bearer-secret" not in json.dumps(receipt)
    assert all(
        request["headers"] == {"Authorization": "Bearer bearer-secret", "Accept": "application/json"}
        for request in client.requests
    )

    ghost_client = FakeClient()
    ghost_client.active_payload = {"run_id": "run-1"}
    ghost_config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="bearer-secret",
        hive_agent_id="agent-1",
        hive_revision="revision-1",
        hive_binary_sha256="c" * 64,
        external_profile_authorized=True,
        http_client=ghost_client,
    )
    ghost_scenario = runtime._scenario_workspace(tmp_path / "ghost-seed", "memory_recall")
    ghost_envelope, ghost_sha256 = runtime._build_same_envelope(ghost_scenario, config=ghost_config)
    ghost_receipt = runtime._run_hive_j4(
        ghost_scenario,
        ghost_envelope,
        ghost_sha256,
        output_dir=tmp_path / "ghost-output",
        config=ghost_config,
    )

    assert ghost_receipt["status"] == "needs_reconciliation"
    active_index = next(
        index for index, request in enumerate(ghost_client.requests) if str(request["path"]).endswith("/runs/active")
    )
    assert not any("/files" in str(request["path"]) for request in ghost_client.requests[active_index + 1 :])

    invalid_call_client = FakeClient()
    invalid_call_client.include_unattributed_call = True
    invalid_call_config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="bearer-secret",
        hive_agent_id="agent-1",
        hive_revision="revision-1",
        hive_binary_sha256="c" * 64,
        external_profile_authorized=True,
        http_client=invalid_call_client,
    )
    invalid_scenario = runtime._scenario_workspace(tmp_path / "invalid-call-seed", "memory_recall")
    invalid_envelope, invalid_sha256 = runtime._build_same_envelope(invalid_scenario, config=invalid_call_config)
    invalid_receipt = runtime._run_hive_j4(
        invalid_scenario,
        invalid_envelope,
        invalid_sha256,
        output_dir=tmp_path / "invalid-call-output",
        config=invalid_call_config,
    )
    assert invalid_receipt["status"] == "attestation_failed"


def test_hive_session_authority_rejects_broad_or_missing_runtime_scope(tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "coding")
    envelope, _ = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    remote_root = "workspace/p08-j4/attempt/coding"

    assert (
        runtime._hive_session_authority(
            {"permission_profile": {"allowed_tools": [], "writable_roots": ["workspace/"]}},
            remote_root=remote_root,
            envelope=envelope,
        )
        is None
    )
    incomplete = runtime._hive_session_authority(
        {
            "permission_profile": {
                "allowed_tools": [
                    "read_file",
                    "write_file",
                    "edit_file",
                    "glob_search",
                    "grep_search",
                ],
                "writable_roots": [remote_root],
            }
        },
        remote_root=remote_root,
        envelope=envelope,
    )
    assert incomplete is None

    authority = runtime._hive_session_authority(
        {
            "permission_profile": {
                "mode": "bypassPermissions",
                "allowed_tools": list(runtime._J4_ALLOWED_TOOLS["hive"]),
                "writable_roots": [remote_root],
                "readable_roots": [remote_root],
                "capability_policy_snapshot": {"session_exact_scope": True},
            }
        },
        remote_root=remote_root,
        envelope=envelope,
    )
    assert authority is not None
    assert authority["effective"]["writable_scope"] == "evaluation_workspace_only"


def test_hive_adapter_stops_before_seed_or_run_when_session_scope_is_unattested(monkeypatch, tmp_path: Path) -> None:
    from urllib.parse import urlparse

    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(runtime.uuid, "uuid4", lambda: SimpleNamespace(hex="attempt-unscoped"))

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self):
            self.requests = []

        def request(self, method, url, **kwargs):
            path = urlparse(url).path
            self.requests.append({"method": method, "path": path, **kwargs})
            if method == "GET" and path == "/api/health":
                return FakeResponse(
                    {
                        "version": "2026.08.31",
                        "components": {"build_identity": {"revision": "revision-1", "sha256": "c" * 64}},
                    }
                )
            if method == "GET" and path == "/api/v1/auth/me":
                return FakeResponse({"id": "user-1"})
            if method == "GET" and path == "/api/v1/agents/agent-1":
                return FakeResponse({"primary_model_id": "model-1", "fallback_model_id": None, "max_tool_rounds": 6})
            if method == "GET" and path == "/api/v1/enterprise/llm-models":
                return FakeResponse(
                    [
                        {
                            "id": "model-1",
                            "provider": "openai-response",
                            "model": "gpt-5.4",
                            "reasoning_effort": "low",
                            "enabled": True,
                        }
                    ]
                )
            if method == "GET" and path == "/api/v1/agents/agent-1/files/":
                return FakeResponse([])
            if method == "POST" and path == "/api/v1/agents/agent-1/sessions":
                return FakeResponse({"id": "session-unscoped"}, status_code=201)
            raise AssertionError(f"unexpected request: {method} {path}")

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    client = FakeClient()
    config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="bearer-secret",
        hive_agent_id="agent-1",
        hive_revision="revision-1",
        hive_binary_sha256="c" * 64,
        external_profile_authorized=True,
        http_client=client,
    )
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=config)

    receipt = runtime._run_hive_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path,
        config=config,
    )

    assert receipt["status"] == "sandbox_unavailable"
    assert receipt["execution"]["session_id"] == "session-unscoped"
    assert "session_authority_attestation" in receipt["parsed"]["schema_errors"]
    assert not any(request["method"] == "PUT" or str(request["path"]).endswith("/runs") for request in client.requests)


def test_hive_adapter_timeout_returns_cancelled_receipt_without_output_readback(monkeypatch, tmp_path: Path) -> None:
    from urllib.parse import urlparse

    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(runtime.uuid, "uuid4", lambda: SimpleNamespace(hex="attempt-timeout"))
    ticks = iter((0.0, 0.0, 2.0, 3.0))
    monkeypatch.setattr(runtime, "monotonic", lambda: next(ticks))
    cancel_calls = []

    def fake_cancel(*args, **kwargs):
        del args
        cancel_calls.append(kwargs)
        return (
            "cancelled",
            [
                {
                    "kind": "run.cancelled",
                    "lifecycle": "cancelled",
                    "scope": {"run_id": "run-1"},
                    "payload": {},
                }
            ],
            {"turns": 0, "tokens": 0, "observed_cost": None, "terminal_status": "cancelled"},
            {"requested": True, "http_status": 202, "fence": "settled"},
        )

    monkeypatch.setattr(runtime, "_hive_cancel_and_fence", fake_cancel)

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self):
            self.files = {}
            self.requests = []
            self.remote_root = ""

        def request(self, method, url, **kwargs):
            path = urlparse(url).path
            params = kwargs.get("params") or {}
            payload = kwargs.get("json") or {}
            self.requests.append({"method": method, "path": path, **kwargs})
            if method == "GET" and path == "/api/health":
                return FakeResponse(
                    {
                        "version": "2026.08.31",
                        "components": {"build_identity": {"revision": "revision-1", "sha256": "c" * 64}},
                    }
                )
            if method == "GET" and path == "/api/v1/auth/me":
                return FakeResponse({"id": "user-1"})
            if method == "GET" and path == "/api/v1/agents/agent-1":
                return FakeResponse({"primary_model_id": "model-1", "fallback_model_id": None, "max_tool_rounds": 6})
            if method == "GET" and path == "/api/v1/enterprise/llm-models":
                return FakeResponse(
                    [
                        {
                            "id": "model-1",
                            "provider": "openai-response",
                            "model": "gpt-5.4",
                            "reasoning_effort": "low",
                            "enabled": True,
                        }
                    ]
                )
            if method == "GET" and path == "/api/v1/agents/agent-1/files/":
                self.remote_root = str(params["path"])
                return FakeResponse(
                    [
                        {"path": name, "is_dir": False}
                        for name in sorted(self.files)
                        if name.startswith(f"{self.remote_root}/")
                    ]
                )
            if method == "POST" and path == "/api/v1/agents/agent-1/sessions":
                return FakeResponse(
                    {
                        "id": "session-1",
                        "permission_profile": {
                            "mode": payload.get("permission_mode"),
                            "allowed_tools": list(runtime._J4_ALLOWED_TOOLS["hive"]),
                            "writable_roots": [self.remote_root],
                            "readable_roots": [self.remote_root],
                            "capability_policy_snapshot": {"session_exact_scope": True},
                        },
                    },
                    status_code=201,
                )
            if method == "PUT" and path == "/api/v1/agents/agent-1/files/content":
                self.files[str(params["path"])] = str(payload["content"])
                return FakeResponse({"status": "ok"})
            if method == "GET" and path == "/api/v1/agents/agent-1/files/content":
                return FakeResponse({"content": self.files[str(params["path"])]})
            if method == "POST" and path == "/api/v1/agents/agent-1/sessions/session-1/runs":
                return FakeResponse({"run_id": "run-1"}, status_code=201)
            raise AssertionError(f"unexpected request: {method} {path}")

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    client = FakeClient()
    config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="bearer-secret",
        hive_agent_id="agent-1",
        hive_revision="revision-1",
        hive_binary_sha256="c" * 64,
        external_profile_authorized=True,
        wall_clock_seconds=1,
        cancel_fence_seconds=1,
        http_client=client,
    )
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=config)

    receipt = runtime._run_hive_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path,
        config=config,
    )

    assert receipt["status"] == "cancelled"
    assert receipt["execution"]["terminal_status"] == "cancelled"
    assert receipt["execution"]["cancel"]["fence"] == "settled"
    assert cancel_calls[0]["attempt_id"] == "attempt-timeout"
    run_index = next(index for index, request in enumerate(client.requests) if str(request["path"]).endswith("/runs"))
    assert not any("/files" in str(request["path"]) for request in client.requests[run_index + 1 :])


def test_runtime_payload_parser_rejects_prefix_and_suffix_text() -> None:
    import pytest

    from app.evals.bakeoff_runtime import extract_runtime_payload

    with pytest.raises(ValueError, match="exactly one JSON object"):
        extract_runtime_payload("freecode", 'log line\n{"status":"success"}')
    with pytest.raises(ValueError, match="exactly one JSON object"):
        extract_runtime_payload("hermes_agent", '{"status":"success"}\ntrailer')


def test_j4_external_scorer_uses_workspace_facts_and_exact_research_fields(tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    coding = runtime._scenario_workspace(tmp_path / "coding-seed", "coding")
    (coding.workspace_dir / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    coding_score = runtime._external_score("coding", coding.workspace_dir)
    assert coding_score["criteria"] == {
        "coding.ast_add": True,
        "coding.execution_assertions": True,
    }

    research = runtime._scenario_workspace(tmp_path / "research-seed", "research")
    result_path = research.workspace_dir / "research_summary.md"
    result_path.write_text(
        "Atlas and 2026-05-01 appear in source_alpha.md and source_beta.md, but this is not a verdict.",
        encoding="utf-8",
    )
    assert runtime._external_score("research", research.workspace_dir)["score"] == 0
    result_path.write_text(
        "Winner: Atlas\nShip date: 2026-05-01\nSources: source_alpha.md, source_beta.md\n",
        encoding="utf-8",
    )
    assert runtime._external_score("research", research.workspace_dir)["score"] == 100


def test_j4_envelope_is_canonical_and_rejects_unsafe_paths(tmp_path: Path) -> None:
    import pytest

    import app.evals.bakeoff_runtime as runtime

    first = runtime._scenario_workspace(tmp_path / "one", "memory_recall")
    second = runtime._scenario_workspace(tmp_path / "two", "memory_recall")
    first_envelope, first_hash = runtime._build_same_envelope(first, config=runtime.J4RuntimeConfig())
    second_envelope, second_hash = runtime._build_same_envelope(second, config=runtime.J4RuntimeConfig())

    assert first_envelope == second_envelope
    assert first_hash == second_hash == runtime._sha256_json(first_envelope)
    assert first_envelope["schema"] == "hive.j4.same_envelope.v1"
    assert first_envelope["workspace"]["clone_policy"] == "immutable_seed_per_runtime"
    assert first_envelope["model"]["fallback_allowed"] is False
    assert first_envelope["model"]["allowed_provider_routes"] == {
        "hive": ["openai-response"],
        "freecode": ["chatgpt-codex"],
        "hermes": ["openai-codex"],
    }
    assert first_envelope["authority"]["writable_scope"] == "evaluation_workspace_only"
    assert first_envelope["authority"]["allowed_tools"]["hive"] == [
        "read_file",
        "write_file",
        "edit_file",
        "glob_search",
        "grep_search",
    ]
    scorer_identity = runtime._scorer_runtime_identity()
    assert first_envelope["scorer"]["source_sha256"] == scorer_identity["source_sha256"]
    assert first_envelope["scorer"]["loaded_code_sha256"] == scorer_identity["loaded_code_sha256"]
    assert json.loads(runtime._canonical_json(first_envelope)) == first_envelope
    with pytest.raises(ValueError, match="Unsafe workspace path"):
        runtime._safe_relative_path("../outside.txt")
    with pytest.raises(ValueError, match="Unsafe workspace path"):
        runtime._safe_relative_path("/absolute.txt")


def test_j4_scorer_freeze_binds_loaded_code_and_canonical_output_dir(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    requested_output = tmp_path / "alias" / ".." / "output"
    canonical_output = requested_output.resolve()
    identity = runtime._scorer_runtime_identity()

    frozen_source, errors = runtime._freeze_scorer_source(canonical_output, identity)

    assert errors == []
    assert frozen_source == canonical_output / "j4_scoring" / "scorer" / "bakeoff_runtime.py"
    assert runtime._scorer_runtime_stable(identity, frozen_source)
    monkeypatch.setattr(runtime, "_external_score", lambda _scenario, _workspace: {})
    assert not runtime._scorer_runtime_stable(identity, frozen_source)


def _completed_test_receipt(
    module,
    runtime_name,
    scenario,
    envelope,
    envelope_sha256,
    output_dir,
    *,
    config,
    prepared,
    memory_answer="cedar-lantern",
):
    workspace_root = module._runtime_workspace_path(output_dir, runtime_name, envelope["envelope_id"])
    before, errors, workspace_identity = module._clone_seed(scenario.workspace_dir, workspace_root)
    assert errors == []
    if scenario.name == "memory_recall":
        (workspace_root / "memory_answer.md").write_text(memory_answer, encoding="utf-8")
    payload = {
        "status": "success",
        "answer": "done",
        "evidence": [],
        "files_created": ["memory_answer.md"],
        "used_parallelism": False,
        "notes": "done",
    }
    artifacts, _artifacts_errors = module._write_j4_artifacts(
        output_dir,
        runtime=runtime_name,
        envelope_id=envelope["envelope_id"],
        stdout=module._canonical_json(payload),
        stderr="",
        transcript="{}",
    )
    workspace = module._workspace_receipt(
        workspace_root,
        before,
        envelope=envelope,
        declared_paths=payload["files_created"],
        workspace_identity=workspace_identity,
    )
    if runtime_name == "hermes":
        binary = deepcopy(prepared.hermes_binary)
        state_root = module._hermes_state_root(workspace_root, str(envelope["envelope_id"])).resolve()
        argv = module._hermes_command(
            workspace_root=workspace_root,
            envelope=envelope,
            python=prepared.hermes_python,
            launcher=prepared.hermes_launcher,
        )
        execution = {
            "chat_spawn_count": 1,
            "argv_sha256": module._sha256_json(argv),
            "transcript_export_spawn_count": 1,
            "cwd": workspace["local_path"],
            "workspace_flag": workspace["local_path"],
            "session_id": "session-1",
            "pre_state_db_absent": True,
            "state_root_owned": True,
            "state_cleanup_verified": True,
            "state_manifest_sha256": module._sha256_json([]),
            "runtime_pre_stable": True,
            "runtime_post_chat_stable": True,
            "runtime_final_stable": True,
            "auth_profile": dict(prepared.hermes_auth_profile),
            "attempt_environment_sha256": module._sha256_json(_expected_hermes_attempt_environment(state_root)),
            "session_attestation": {"ok": True, "session_id": "session-1", "lineage_count": 1},
        }
    elif runtime_name == "freecode":
        runtime_root = workspace_root.parent / "runtime"
        artifact = prepared.freecode_manifest["artifact"]
        source = prepared.freecode_manifest["source"]
        binary = {
            "path": str(runtime_root / "freecode"),
            "version": artifact["version"],
            "sha256": artifact["sha256"],
            "revision": source["revision"],
            "runtime_sha256": module._freecode_runtime_sha256(prepared),
            "components": {
                "build_manifest": {
                    "sha256": prepared.freecode_manifest_sha256,
                    "schema": module.FREECODE_BUILD_MANIFEST_SCHEMA,
                },
                "fresh_build_receipt": dict(prepared.freecode_build_receipt),
                "source": dict(source),
                "authority_guard": {
                    "path": str(runtime_root / "freecode_j4_hook.py"),
                    "sha256": prepared.freecode_hook_sha256,
                    "python_path": str(prepared.freecode_hook_python),
                    "python_sha256": prepared.freecode_hook_python_sha256,
                    "python_environment_sha256": prepared.freecode_hook_python_environment_sha256,
                },
            },
        }
        argv = module._freecode_command(
            prompt=scenario.prompt,
            envelope=envelope,
            workspace_root=workspace_root,
            binary=runtime_root / "freecode",
            hook=runtime_root / "freecode_j4_hook.py",
            hook_python=prepared.freecode_hook_python,
        )
        execution = {
            "chat_spawn_count": 1,
            "attempt_roots_owned": True,
            "state_cleanup_verified": True,
            "argv_sha256": module._sha256_json(argv),
            "cwd": workspace["local_path"],
            "runtime_pre_sha256": binary["sha256"],
            "runtime_post_sha256": binary["sha256"],
            "guard_pre_sha256": binary["components"]["authority_guard"]["sha256"],
            "guard_post_sha256": binary["components"]["authority_guard"]["sha256"],
            "guard_python_pre_sha256": binary["components"]["authority_guard"]["python_sha256"],
            "guard_python_post_sha256": binary["components"]["authority_guard"]["python_sha256"],
            "guard_python_environment_pre_sha256": binary["components"]["authority_guard"]["python_environment_sha256"],
            "guard_python_environment_post_sha256": binary["components"]["authority_guard"][
                "python_environment_sha256"
            ],
            "hook_log": {"valid": True, "sha256": artifacts["transcript"]["sha256"]},
        }
    else:
        binary = {
            "path": str(config.hive_base_url),
            "version": "1.0",
            "sha256": str(config.hive_binary_sha256),
            "revision": str(config.hive_revision),
        }
        argv = [runtime_name]
        execution = {
            "attempt_id": "test-attempt",
            "session_id": "session-1",
            "run_id": "run-1",
            "remote_root": f"workspace/p08-j4/test-attempt/{envelope['envelope_id']}",
            "terminal_status": "completed",
            "active_fence": {"status": "settled", "http_status": 200, "active_run_id": None},
        }
    return module._base_receipt(
        runtime=runtime_name,
        binary=binary,
        envelope=envelope,
        envelope_sha256=envelope_sha256,
        status="completed",
        argv=argv,
        duration_ms=1,
        exit_code=0,
        workspace=workspace,
        artifacts=artifacts,
        parsed_payload=payload,
        effective_model="gpt-5.4",
        effective_provider=envelope["model"]["allowed_provider_routes"][runtime_name][0],
        fallback_observed=False,
        attestation_source="test.attestation",
        turns=1,
        effective_resources={
            "max_tool_rounds": envelope["resources"]["max_tool_rounds"],
            "wall_clock_seconds": envelope["resources"]["wall_clock_seconds"],
            "reasoning_effort": envelope["model"]["reasoning_effort"],
        },
        resource_sources={
            "max_tool_rounds": "test.argv",
            "wall_clock_seconds": "test.timeout",
            "reasoning_effort": "test.argv",
        },
        authority={
            "requested": {
                "allowed_tools": envelope["authority"]["allowed_tools"][runtime_name],
                "readable_scope": "evaluation_workspace_only",
                "writable_scope": "evaluation_workspace_only",
            },
            "effective": {
                "allowed_tools": envelope["authority"]["allowed_tools"][runtime_name],
                "readable_scope": "evaluation_workspace_only",
                "writable_scope": "evaluation_workspace_only",
            },
            "sources": {
                "allowed_tools": "test.argv",
                "readable_scope": "test.sandbox",
                "writable_scope": "test.sandbox",
            },
            "sandbox": {"status": "enforced", "provider": "test"},
        },
        route_attestation={
            "call_count": 1,
            "routes": [
                {
                    "model": "gpt-5.4",
                    "provider": envelope["model"]["allowed_provider_routes"][runtime_name][0],
                }
            ],
            "source": "test.attestation",
            **(
                {
                    "tool_names": list(envelope["authority"]["allowed_tools"]["hive"]),
                    "tool_schema_sha256": "d" * 64,
                }
                if runtime_name == "hive"
                else {}
            ),
        },
        execution=execution,
    )


def test_receipt_hard_gates_reject_zero_actual_provider_calls(tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    prepared = _prepared_j4_runtimes(runtime, tmp_path)
    config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="token",
        hive_agent_id="agent-id",
        hive_revision="revision",
        hive_binary_sha256="c" * 64,
        external_profile_authorized=True,
    )
    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=config)
    receipt = _completed_test_receipt(
        runtime,
        "freecode",
        scenario,
        envelope,
        envelope_sha256,
        tmp_path,
        config=config,
        prepared=prepared,
    )
    receipt["route_attestation"]["call_count"] = 0
    receipt["turns"] = 0

    assert "provider_call_evidence" in runtime._receipt_blockers(
        receipt,
        envelope,
        envelope_sha256,
        output_dir=tmp_path,
        config=config,
        prepared=prepared,
        expected_prompt=scenario.prompt,
    )

    hive_output = tmp_path / "hive"
    receipt = _completed_test_receipt(
        runtime,
        "hive",
        scenario,
        envelope,
        envelope_sha256,
        hive_output,
        config=config,
        prepared=prepared,
    )
    receipt["execution"].pop("active_fence")
    assert "execution_refs" in runtime._receipt_blockers(
        receipt,
        envelope,
        envelope_sha256,
        output_dir=hive_output,
        config=config,
        prepared=prepared,
        expected_prompt=scenario.prompt,
    )

    identity_output = tmp_path / "identity"
    receipt = _completed_test_receipt(
        runtime,
        "hive",
        scenario,
        envelope,
        envelope_sha256,
        identity_output,
        config=config,
        prepared=prepared,
    )
    receipt["route_attestation"]["tool_schema_sha256"] = None
    assert "provider_call_evidence" in runtime._receipt_blockers(
        receipt,
        envelope,
        envelope_sha256,
        output_dir=identity_output,
        config=config,
        prepared=prepared,
        expected_prompt=scenario.prompt,
    )

    hermes_output = tmp_path / "hermes"
    receipt = _completed_test_receipt(
        runtime,
        "hermes",
        scenario,
        envelope,
        envelope_sha256,
        hermes_output,
        config=config,
        prepared=prepared,
    )
    receipt["binary"]["components"]["source"]["clean"] = False
    blockers = runtime._receipt_blockers(
        receipt,
        envelope,
        envelope_sha256,
        output_dir=hermes_output,
        config=config,
        prepared=prepared,
        expected_prompt=scenario.prompt,
    )
    assert "hermes_expected_runtime_identity" in blockers
    assert "hermes_runtime_identity" in blockers


def test_same_envelope_missing_authority_is_typed_empty_and_invokes_nothing(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    def must_not_run(*args, **kwargs):
        raise AssertionError("adapter must not run without explicit authority")

    monkeypatch.setattr(runtime, "_run_hive_j4", must_not_run)
    monkeypatch.setattr(runtime, "_run_freecode_j4", must_not_run)
    monkeypatch.setattr(runtime, "_run_hermes_j4", must_not_run)

    report = runtime.run_same_envelope_bakeoff(output_dir=tmp_path, config=runtime.J4RuntimeConfig())

    assert report["benchmark_complete"] is False
    assert report["comparison"]["status"] == "blocked"
    assert report["comparison"]["scores"] == {}
    assert report["scenario_scores"] == {}
    assert report["receipts"] == []


def test_hive_adapter_rejects_caller_claimed_identity_without_server_proof(tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"version": "2026.08.31", "components": {}}

    class FakeClient:
        def __init__(self):
            self.requests = []

        def request(self, method, url, **kwargs):
            self.requests.append((method, url, kwargs))
            return FakeResponse()

    client = FakeClient()
    config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="explicit-token",
        hive_agent_id="agent-id",
        hive_revision="caller-claimed-revision",
        hive_binary_sha256="c" * 64,
        external_profile_authorized=True,
        http_client=client,
    )
    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=config)

    receipt = runtime._run_hive_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path,
        config=config,
    )

    assert receipt["status"] == "resource_unavailable"
    assert "hive_build_identity_unavailable" in receipt["parsed"]["schema_errors"]
    assert receipt["binary"]["revision"] == ""
    assert receipt["binary"]["sha256"] == ""
    assert len(client.requests) == 1


def test_hive_adapter_validates_caller_expectation_against_server_identity(tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "version": "2026.08.31",
                "components": {"build_identity": {"revision": "server-revision", "sha256": "d" * 64}},
            }

    class FakeClient:
        def request(self, _method, _url, **_kwargs):
            return FakeResponse()

    config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="explicit-token",
        hive_agent_id="agent-id",
        hive_revision="caller-revision",
        hive_binary_sha256="c" * 64,
        external_profile_authorized=True,
        http_client=FakeClient(),
    )
    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=config)

    receipt = runtime._run_hive_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path,
        config=config,
    )

    assert receipt["status"] == "attestation_failed"
    assert "hive_build_identity_mismatch" in receipt["parsed"]["schema_errors"]
    assert receipt["binary"]["revision"] == "server-revision"
    assert receipt["binary"]["sha256"] == "d" * 64


def test_same_envelope_scores_only_after_all_hard_gates(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(runtime, "_SCENARIOS", ("memory_recall",))
    requested_output = tmp_path / "path-alias" / ".." / "output"
    prepared_runtimes = _prepared_j4_runtimes(runtime, tmp_path)
    monkeypatch.setattr(runtime, "_prepare_j4_runtimes", lambda _config: (prepared_runtimes, []))
    monkeypatch.setattr(runtime, "_prepared_runtime_blockers", lambda _prepared, _config: [])

    def adapter(runtime_name):
        def run(scenario, envelope, envelope_sha256, *, output_dir, config, prepared=None):
            assert prepared is (None if runtime_name == "hive" else prepared_runtimes)
            assert output_dir == requested_output.resolve()
            return _completed_test_receipt(
                runtime,
                runtime_name,
                scenario,
                envelope,
                envelope_sha256,
                output_dir,
                config=config,
                prepared=prepared_runtimes,
            )

        return run

    monkeypatch.setattr(runtime, "_run_hive_j4", adapter("hive"))
    monkeypatch.setattr(runtime, "_run_freecode_j4", adapter("freecode"))
    monkeypatch.setattr(runtime, "_run_hermes_j4", adapter("hermes"))
    config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="secret-token",
        hive_agent_id="agent-id",
        hive_revision="revision",
        hive_binary_sha256="c" * 64,
        external_profile_authorized=True,
    )

    report = runtime.run_same_envelope_bakeoff(output_dir=requested_output, config=config)

    assert report["benchmark_complete"] is True
    assert report["acceptance_ready"] is True
    assert report["comparison"]["status"] == "completed"
    assert report["comparison"]["scores"] == {"hive": 100.0, "freecode": 100.0, "hermes": 100.0}
    assert report["comparison"]["blockers"] == []
    assert report["comparison"]["acceptance"]["hive_not_weaker"] is True
    assert set(report["scenario_scores"]["memory_recall"]) == {"hive", "freecode", "hermes"}
    assert all(receipt["score"]["score"] == 100 for receipt in report["receipts"])
    assert report["scorer_artifact"]["path"] == str(
        requested_output.resolve() / "j4_scoring" / "scorer" / "bakeoff_runtime.py"
    )
    assert "secret-token" not in json.dumps(report)


def test_same_envelope_benchmark_can_complete_but_hive_weaker_is_not_accepted(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(runtime, "_SCENARIOS", ("memory_recall",))
    prepared_runtimes = _prepared_j4_runtimes(runtime, tmp_path)
    monkeypatch.setattr(runtime, "_prepare_j4_runtimes", lambda _config: (prepared_runtimes, []))
    monkeypatch.setattr(runtime, "_prepared_runtime_blockers", lambda _prepared, _config: [])

    def adapter(runtime_name):
        def run(scenario, envelope, envelope_sha256, *, output_dir, config, prepared=None):
            assert prepared is (None if runtime_name == "hive" else prepared_runtimes)
            return _completed_test_receipt(
                runtime,
                runtime_name,
                scenario,
                envelope,
                envelope_sha256,
                output_dir,
                config=config,
                prepared=prepared_runtimes,
                memory_answer="incorrect" if runtime_name == "hive" else "cedar-lantern",
            )

        return run

    monkeypatch.setattr(runtime, "_run_hive_j4", adapter("hive"))
    monkeypatch.setattr(runtime, "_run_freecode_j4", adapter("freecode"))
    monkeypatch.setattr(runtime, "_run_hermes_j4", adapter("hermes"))

    config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="token",
        hive_agent_id="agent-id",
        hive_revision="revision",
        hive_binary_sha256="c" * 64,
        external_profile_authorized=True,
    )

    report = runtime.run_same_envelope_bakeoff(output_dir=tmp_path, config=config)

    assert report["benchmark_complete"] is True
    assert report["acceptance_ready"] is False
    assert report["comparison"]["acceptance"]["hive_not_weaker"] is False
    assert (
        report["comparison"]["acceptance"]["comparisons"]["memory_recall.memory.exact_bytes"]["hive_not_weaker"]
        is False
    )


def test_same_envelope_mismatch_clears_every_score(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(runtime, "_SCENARIOS", ("memory_recall",))
    prepared_runtimes = _prepared_j4_runtimes(runtime, tmp_path)
    monkeypatch.setattr(runtime, "_prepare_j4_runtimes", lambda _config: (prepared_runtimes, []))
    monkeypatch.setattr(runtime, "_prepared_runtime_blockers", lambda _prepared, _config: [])

    def adapter(runtime_name):
        def run(scenario, envelope, envelope_sha256, *, output_dir, config, prepared=None):
            assert prepared is (None if runtime_name == "hive" else prepared_runtimes)
            receipt = _completed_test_receipt(
                runtime,
                runtime_name,
                scenario,
                envelope,
                envelope_sha256,
                output_dir,
                config=config,
                prepared=prepared_runtimes,
            )
            if runtime_name == "hermes":
                receipt["effective_model"] = "fallback-model"
                receipt["fallback_observed"] = True
                receipt["resources"]["effective"]["max_tool_rounds"] += 1
            return receipt

        return run

    monkeypatch.setattr(runtime, "_run_hive_j4", adapter("hive"))
    monkeypatch.setattr(runtime, "_run_freecode_j4", adapter("freecode"))
    monkeypatch.setattr(runtime, "_run_hermes_j4", adapter("hermes"))
    config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="token",
        hive_agent_id="agent-id",
        hive_revision="revision",
        hive_binary_sha256="c" * 64,
        external_profile_authorized=True,
    )

    report = runtime.run_same_envelope_bakeoff(output_dir=tmp_path, config=config)

    assert report["benchmark_complete"] is False
    assert report["comparison"]["scores"] == {}
    assert report["scenario_scores"] == {}
    assert all(receipt["score"] is None for receipt in report["receipts"])
    assert {blocker["code"] for blocker in report["comparison"]["blockers"]} >= {
        "effective_model",
        "fallback_observed",
        "hard_common_resources",
    }


def test_auth_failure_and_scorer_drift_preserve_typed_post_run_status(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(runtime, "_SCENARIOS", ("coding", "review"))
    prepared_runtimes = _prepared_j4_runtimes(runtime, tmp_path)
    monkeypatch.setattr(runtime, "_prepare_j4_runtimes", lambda _config: (prepared_runtimes, []))
    monkeypatch.setattr(runtime, "_prepared_runtime_blockers", lambda _prepared, _config: [])
    calls = {name: 0 for name in ("hive", "freecode", "hermes")}

    def adapter(runtime_name):
        def run(scenario, envelope, envelope_sha256, *, output_dir, config, prepared=None):
            del scenario, output_dir, config
            assert prepared is (None if runtime_name == "hive" else prepared_runtimes)
            calls[runtime_name] += 1
            return {
                "runtime": runtime_name,
                "status": "auth_required" if runtime_name == "freecode" else "failed",
                "envelope_sha256": envelope_sha256,
                "resources": {"effective": envelope["resources"]},
            }

        return run

    monkeypatch.setattr(runtime, "_run_hive_j4", adapter("hive"))
    monkeypatch.setattr(runtime, "_run_freecode_j4", adapter("freecode"))
    monkeypatch.setattr(runtime, "_run_hermes_j4", adapter("hermes"))
    config = runtime.J4RuntimeConfig(
        hive_base_url="https://hive.example",
        hive_bearer="token",
        hive_agent_id="agent-id",
        hive_revision="revision",
        hive_binary_sha256="c" * 64,
        external_profile_authorized=True,
    )

    report = runtime.run_same_envelope_bakeoff(output_dir=tmp_path, config=config)

    assert report["benchmark_complete"] is False
    assert calls == {"hive": 1, "freecode": 1, "hermes": 1}
    assert report["transport"] == "same_envelope_live"
    assert report["auth_status"] == "auth_required"
    assert report["runtime"] == {
        "status": "completed_with_blockers",
        "observed_statuses": {
            "hive": ["failed"],
            "freecode": ["auth_required"],
            "hermes": ["failed"],
        },
    }
    assert {
        "code": "runtime_identity_drift",
        "runtime": "external_scorer",
        "scenario": "review",
        "phase": "scenario_pre",
    } in report["comparison"]["blockers"]


def test_workspace_receipt_keeps_unobserved_clone_identity_unknown(tmp_path: Path) -> None:
    """A clone whose identity was never observed must stay unknown evidence.

    _clone_seed returns workspace_identity=None when its post-copy lstat
    fails; ownership is then unverifiable, so the receipt must not traverse
    the root and publish a fresh traversal-time identity that downstream
    scoring would treat as verified.
    """
    from app.evals import bakeoff_runtime as runtime

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "seed.txt").write_text("seed bytes", encoding="utf-8")
    envelope = {"workspace": {"logical_root": "workspace", "max_files": 20, "max_bytes": 4096}}

    receipt = runtime._workspace_receipt(workspace, [], envelope=envelope, workspace_identity=None)

    assert receipt["root_identity"] is None
    assert receipt["after_manifest"] is None
    assert receipt["after_sha256"] is None
    assert receipt["boundary_ok"] is None
    assert receipt["boundary_errors"] == ["workspace_unobserved:workspace_identity_unobserved"]


def test_workspace_receipt_publishes_the_verified_identity_not_a_replacement(tmp_path: Path, monkeypatch) -> None:
    """The published root_identity is the tuple verified before traversal.

    A same-path replacement created while the after-manifest walks must not
    lend its fresh stat to the receipt: scoring compares this tuple against
    the live source, so adopting an unverified replacement identity would
    make the identity check self-referential.
    """
    from app.evals import bakeoff_runtime as runtime

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "seed.txt").write_text("seed bytes", encoding="utf-8")
    details = workspace.stat()
    verified_identity = (details.st_dev, details.st_ino)
    original_manifest = runtime._manifest

    def manifest_then_swap(root: Path):
        result = original_manifest(root)
        root.rename(tmp_path / "original-workspace")
        root.mkdir()
        (root / "replacement.txt").write_text("replacement bytes", encoding="utf-8")
        return result

    monkeypatch.setattr(runtime, "_manifest", manifest_then_swap)
    envelope = {"workspace": {"logical_root": "workspace", "max_files": 20, "max_bytes": 4096}}

    receipt = runtime._workspace_receipt(workspace, [], envelope=envelope, workspace_identity=verified_identity)

    replacement_details = workspace.stat()
    assert receipt["root_identity"] == {"st_dev": verified_identity[0], "st_ino": verified_identity[1]}
    assert receipt["root_identity"] != {
        "st_dev": replacement_details.st_dev,
        "st_ino": replacement_details.st_ino,
    }
