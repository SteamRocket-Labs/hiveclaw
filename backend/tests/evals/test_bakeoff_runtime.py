from __future__ import annotations

import subprocess
from pathlib import Path


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


def test_run_runtime_bakeoff_falls_back_to_repo_evidence_when_auth_is_missing(monkeypatch, tmp_path: Path) -> None:
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
    monkeypatch.setattr(
        runtime,
        "_repo_evidence_report",
        lambda target: {
            "kind": "bakeoff",
            "transport": "repo_evidence",
            "repo_root": f"/tmp/{target}",
            "scenarios": {
                "coding": {
                    "ready": True,
                    "score": 95,
                    "transcript": "repo evidence",
                    "rubric": "coding assistant maturity",
                    "score_breakdown": {"repo_found": True},
                }
            },
        },
    )

    report = runtime.run_runtime_bakeoff("claude_code", output_dir=tmp_path)

    assert report["transport"] == "repo_evidence_only"
    assert report["runtime"]["status"] == "auth_required"
    assert report["auth_status"] == "auth_required"
    assert report["fallback_used"] is True
    assert report["benchmark_complete"] is False
    assert report["fallback"]["transport"] == "repo_evidence"
    assert report["scenarios"]["coding"]["score"] == 95
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


def test_resolve_bakeoff_repo_root_prefers_environment_override(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    repo_root = tmp_path / "claude-code"
    repo_root.mkdir()
    monkeypatch.setenv("HIVE_BAKEOFF_CLAUDE_CODE_ROOT", str(repo_root))

    assert runtime._resolve_bakeoff_repo_root("claude_code") == repo_root.resolve()


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
    assert report["fallback_used"] is False
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

    class _CompletedProcess:
        returncode = 0
        stdout = '{"status":"success"}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return _CompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)

    runtime._run_process(
        ["claude", "-p", "Return ok"],
        cwd=tmp_path,
        timeout_seconds=5,
    )

    env = captured.get("env")
    assert isinstance(env, dict)
    assert env.get("CLAUDE_CODE_SIMPLE") != "1"
