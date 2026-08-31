from __future__ import annotations

import subprocess
import json
from pathlib import Path
from types import SimpleNamespace


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


def test_j4_run_process_fails_closed_when_workspace_sandbox_is_unavailable(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_build_workspace_sandbox_command",
        lambda command, cwd, env: (None, [], {"status": "unavailable", "provider": None, "reason": "missing"}),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
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


def test_j4_cli_commands_are_argv_only_and_pin_the_same_model(tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "coding")
    envelope, _digest = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    freecode_binary = tmp_path / "bin" / "freecode"

    freecode = runtime._freecode_command(prompt=scenario.prompt, envelope=envelope, binary=freecode_binary)
    hermes = runtime._hermes_command(workspace_root=tmp_path / "workspace", envelope=envelope)

    assert freecode[0] == str(freecode_binary)
    assert freecode[freecode.index("--model") + 1] == "gpt-5.4"
    assert freecode[freecode.index("--setting-sources") + 1] == ""
    assert "--bare" not in freecode
    assert hermes[:3] == [str(runtime.HERMES_BINARY.resolve()), "chat", "--query-file"]
    assert hermes[hermes.index("--provider") + 1] == "openai-codex"
    assert hermes[hermes.index("--reasoning") + 1] == "low"
    assert hermes[hermes.index("--source") + 1] == "p08-j4"


def test_freecode_binary_uses_explicit_override_and_has_no_personal_fallback(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    configured = tmp_path / "freecode"
    monkeypatch.setenv(runtime.FREECODE_BINARY_ENV, str(configured))
    assert runtime._freecode_binary() == configured.resolve()

    monkeypatch.delenv(runtime.FREECODE_BINARY_ENV)
    monkeypatch.setattr(runtime, "_which", lambda _name: None)
    assert runtime._freecode_binary() is None

    monkeypatch.setattr(
        runtime,
        "_run_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("missing CLI must not execute")),
    )
    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    config = runtime.J4RuntimeConfig(external_profile_authorized=True)
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=config)
    receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path / "output",
        config=config,
    )
    assert receipt["status"] == "cli_unavailable"
    assert receipt["binary"] == {"path": "", "version": "", "sha256": "", "revision": ""}


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


def test_freecode_attestation_requires_an_output_side_provider_namespace() -> None:
    import app.evals.bakeoff_runtime as runtime

    namespaced = runtime._freecode_attestation({"modelUsage": {"openai/gpt-5.4": {"inputTokens": 10}}, "num_turns": 1})
    bare = runtime._freecode_attestation({"modelUsage": {"gpt-5.4": {"inputTokens": 10}}, "num_turns": 1})

    assert namespaced[:3] == ("gpt-5.4", "chatgpt-codex", False)
    assert bare[:3] == ("gpt-5.4", None, True)


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
    calls: list[dict[str, object]] = []
    model_usage_key = ["openai/gpt-5.4"]
    freecode_binary = tmp_path / "freecode"
    freecode_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv(runtime.FREECODE_BINARY_ENV, str(freecode_binary))

    monkeypatch.setattr(
        runtime,
        "_binary_identity",
        lambda name, path: {
            "path": str(path.resolve()),
            "version": "free-code 1.0",
            "sha256": "a" * 64,
            "revision": "revision",
        },
    )

    def fake_run(command, cwd, timeout_seconds, *, env_overrides=None, require_workspace_sandbox=False):
        calls.append(
            {
                "command": command,
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "env_overrides": env_overrides,
                "require_workspace_sandbox": require_workspace_sandbox,
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
    )

    assert receipt["status"] == "completed"
    assert receipt["effective_model"] == "gpt-5.4"
    assert receipt["effective_provider"] == "chatgpt-codex"
    assert receipt["fallback_observed"] is False
    assert receipt["tokens"] == 15
    assert receipt["score"] is None
    assert calls[0]["env_overrides"] == {"CLAUDE_CODE_USE_OPENAI": "1"}
    assert calls[0]["command"] == receipt["argv"]
    assert calls[0]["require_workspace_sandbox"] is True
    assert receipt["route_attestation"]["call_count"] == 1
    assert receipt["route_attestation"]["count_semantics"] == "minimum_observed"
    assert receipt["authority"]["sandbox"]["status"] == "enforced"
    assert receipt["resources"]["effective"] == receipt["resources"]["requested"]
    assert isinstance(receipt["argv"], list)

    model_usage_key[0] = "gpt-5.4"
    bare_provider_receipt = runtime._run_freecode_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path / "bare-provider",
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
    )
    assert bare_provider_receipt["status"] == "attestation_failed"
    assert bare_provider_receipt["effective_provider"] is None


def test_hermes_j4_adapter_exports_only_anchored_session_and_attests_route(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    commands: list[list[str]] = []
    include_unattributed_call = [False]
    monkeypatch.setattr(
        runtime,
        "_binary_identity",
        lambda name, path: {
            "path": str(path.resolve()),
            "version": "Hermes Agent v0.20.6 · upstream 9eb832aa · local 7eee066c",
            "sha256": "b" * 64,
            "revision": "upstream:9eb832aa;local:7eee066c",
        },
    )

    def fake_run(command, cwd, timeout_seconds, *, env_overrides=None, require_workspace_sandbox=False):
        del timeout_seconds, env_overrides
        assert require_workspace_sandbox is True
        commands.append(command)
        if command[1:3] == ["sessions", "export"]:
            valid_segment = {
                "model": "openai/gpt-5.4",
                "billing_provider": "openai-codex",
                "model_config": {"provider": "openai-codex", "model": "gpt-5.4"},
                "api_call_count": 2,
                "input_tokens": 12,
                "output_tokens": 4,
                "estimated_cost_usd": 0.02,
            }
            transcript = (
                {"segments": [valid_segment, {"api_call_count": 1, "input_tokens": 5}]}
                if include_unattributed_call[0]
                else valid_segment
            )
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
    assert commands[1] == [
        str(runtime.HERMES_BINARY.resolve()),
        "sessions",
        "export",
        "-",
        "--format",
        "jsonl",
        "--session-id",
        "sess-1",
        "--redact",
    ]
    assert receipt["argv"] == commands[0]

    include_unattributed_call[0] = True
    unattributed_receipt = runtime._run_hermes_j4(
        scenario,
        envelope,
        envelope_sha256,
        output_dir=tmp_path / "unattributed-call",
        config=runtime.J4RuntimeConfig(external_profile_authorized=True),
    )
    assert unattributed_receipt["status"] == "attestation_failed"


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
    assert json.loads(runtime._canonical_json(first_envelope)) == first_envelope
    with pytest.raises(ValueError, match="Unsafe workspace path"):
        runtime._safe_relative_path("../outside.txt")
    with pytest.raises(ValueError, match="Unsafe workspace path"):
        runtime._safe_relative_path("/absolute.txt")


def _completed_test_receipt(module, runtime_name, scenario, envelope, envelope_sha256, output_dir):
    workspace_root = module._runtime_workspace_path(output_dir, runtime_name, envelope["envelope_id"])
    before, errors = module._clone_seed(scenario.workspace_dir, workspace_root)
    assert errors == []
    if scenario.name == "memory_recall":
        (workspace_root / "memory_answer.md").write_text("cedar-lantern", encoding="utf-8")
    payload = {
        "status": "success",
        "answer": "done",
        "evidence": [],
        "files_created": ["memory_answer.md"],
        "used_parallelism": False,
        "notes": "done",
    }
    artifacts = module._write_j4_artifacts(
        output_dir,
        runtime=runtime_name,
        envelope_id=envelope["envelope_id"],
        stdout=module._canonical_json(payload),
        stderr="",
        transcript="{}",
    )
    return module._base_receipt(
        runtime=runtime_name,
        binary={
            "path": f"/tmp/{runtime_name}",
            "version": "1.0",
            "sha256": "a" * 64,
            "revision": "revision",
        },
        envelope=envelope,
        envelope_sha256=envelope_sha256,
        status="completed",
        argv=[runtime_name],
        duration_ms=1,
        exit_code=0,
        workspace=module._workspace_receipt(
            workspace_root,
            before,
            envelope=envelope,
            declared_paths=payload["files_created"],
        ),
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
                "writable_scope": "evaluation_workspace_only",
            },
            "effective": {
                "allowed_tools": envelope["authority"]["allowed_tools"][runtime_name],
                "writable_scope": "evaluation_workspace_only",
            },
            "sources": {"allowed_tools": "test.argv", "writable_scope": "test.sandbox"},
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
        execution=(
            {
                "attempt_id": "test-attempt",
                "session_id": "session-1",
                "run_id": "run-1",
                "remote_root": f"workspace/p08-j4/test-attempt/{envelope['envelope_id']}",
                "terminal_status": "completed",
                "active_fence": {"status": "settled", "http_status": 200, "active_run_id": None},
            }
            if runtime_name == "hive"
            else {}
        ),
    )


def test_receipt_hard_gates_reject_zero_actual_provider_calls(tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    scenario = runtime._scenario_workspace(tmp_path / "seed", "memory_recall")
    envelope, envelope_sha256 = runtime._build_same_envelope(scenario, config=runtime.J4RuntimeConfig())
    receipt = _completed_test_receipt(runtime, "freecode", scenario, envelope, envelope_sha256, tmp_path)
    receipt["route_attestation"]["call_count"] = 0
    receipt["turns"] = 0

    assert "provider_call_evidence" in runtime._receipt_blockers(receipt, envelope, envelope_sha256)

    receipt = _completed_test_receipt(runtime, "hive", scenario, envelope, envelope_sha256, tmp_path / "hive")
    receipt["execution"].pop("active_fence")
    assert "execution_refs" in runtime._receipt_blockers(receipt, envelope, envelope_sha256)

    receipt = _completed_test_receipt(runtime, "hive", scenario, envelope, envelope_sha256, tmp_path / "identity")
    receipt["route_attestation"]["tool_schema_sha256"] = None
    assert "provider_call_evidence" in runtime._receipt_blockers(receipt, envelope, envelope_sha256)


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

    def adapter(runtime_name):
        def run(scenario, envelope, envelope_sha256, *, output_dir, config):
            del config
            return _completed_test_receipt(
                runtime,
                runtime_name,
                scenario,
                envelope,
                envelope_sha256,
                output_dir,
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

    report = runtime.run_same_envelope_bakeoff(output_dir=tmp_path, config=config)

    assert report["benchmark_complete"] is True
    assert report["acceptance_ready"] is True
    assert report["comparison"]["status"] == "completed"
    assert report["comparison"]["scores"] == {"hive": 100.0, "freecode": 100.0, "hermes": 100.0}
    assert report["comparison"]["blockers"] == []
    assert report["comparison"]["acceptance"]["hive_not_weaker"] is True
    assert set(report["scenario_scores"]["memory_recall"]) == {"hive", "freecode", "hermes"}
    assert all(receipt["score"]["score"] == 100 for receipt in report["receipts"])
    assert "secret-token" not in json.dumps(report)


def test_same_envelope_benchmark_can_complete_but_hive_weaker_is_not_accepted(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(runtime, "_SCENARIOS", ("memory_recall",))

    def adapter(runtime_name):
        def run(scenario, envelope, envelope_sha256, *, output_dir, config):
            del config
            return _completed_test_receipt(
                runtime,
                runtime_name,
                scenario,
                envelope,
                envelope_sha256,
                output_dir,
            )

        return run

    monkeypatch.setattr(runtime, "_run_hive_j4", adapter("hive"))
    monkeypatch.setattr(runtime, "_run_freecode_j4", adapter("freecode"))
    monkeypatch.setattr(runtime, "_run_hermes_j4", adapter("hermes"))

    def score(_scenario_name, workspace_root):
        ready = "/hive/" not in str(workspace_root)
        return {
            "score": 100 if ready else 0,
            "ready": ready,
            "criteria": {"memory.exact_bytes": ready},
            "source": "test.external",
        }

    monkeypatch.setattr(runtime, "_external_score", score)
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

    def adapter(runtime_name):
        def run(scenario, envelope, envelope_sha256, *, output_dir, config):
            del config
            receipt = _completed_test_receipt(
                runtime,
                runtime_name,
                scenario,
                envelope,
                envelope_sha256,
                output_dir,
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


def test_auth_failure_on_first_formal_scenario_stops_only_that_runtime(monkeypatch, tmp_path: Path) -> None:
    import app.evals.bakeoff_runtime as runtime

    monkeypatch.setattr(runtime, "_SCENARIOS", ("coding", "review"))
    calls = {name: 0 for name in ("hive", "freecode", "hermes")}

    def adapter(runtime_name):
        def run(scenario, envelope, envelope_sha256, *, output_dir, config):
            del scenario, output_dir, config
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
    assert calls == {"hive": 2, "freecode": 1, "hermes": 2}
