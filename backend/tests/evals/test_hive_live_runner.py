"""E2/G0/G1: Hive agent-core live behavior runner + ledger wiring + grader (red first)."""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.evals.hive_live_runner import (
    BEHAVIOR_REPORT_STDOUT_MARKER,
    TRUSTED_LIVE_TRANSPORTS,
    behavior_eval_passed,
    build_invoke_agent_runner,
    main,
    record_behavior_eval_run,
    resolve_production_eval_runtime,
    run_hive_behavior_eval,
    write_behavior_report,
)
from app.services.evolution_verification import run_evolution_verification


# ---- fake agent runners (stand in for invoke_agent without DB/LLM) ----


async def _good_coding_runner(prompt: str, workspace_dir: Path) -> dict:
    (workspace_dir / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return {
        "status": "success",
        "answer": "fixed add()",
        "evidence": ["calculator.py"],
        "files_created": ["calculator.py"],
        "used_parallelism": False,
        "notes": "done",
    }


async def _bad_coding_runner(prompt: str, workspace_dir: Path) -> dict:
    return {
        "status": "failed",
        "answer": "could not fix",
        "evidence": [],
        "files_created": [],
        "used_parallelism": False,
        "notes": "",
    }


async def _raising_runner(prompt: str, workspace_dir: Path) -> dict:
    raise RuntimeError("agent core unavailable (no DB/LLM)")


def _live_report(
    transport: str = "hive_live", *, ready: bool = True, complete: bool = True, fallback: bool = False
) -> dict:
    return {
        "kind": "behavior_eval",
        "transport": transport,
        "benchmark_complete": complete,
        "fallback_used": fallback,
        "scenarios": {
            "coding": {"ready": ready, "score": 100 if ready else 0},
        },
    }


# ---- behavior_eval_passed (the fallback-not-passed gate, security-critical) ----


def test_behavior_eval_passed_true_for_complete_hive_live() -> None:
    assert behavior_eval_passed(_live_report("hive_live")) is True


def test_behavior_eval_passed_false_for_untrusted_transport() -> None:
    # repo_evidence is a diagnostic fallback, never a passing gate signal
    assert behavior_eval_passed(_live_report("repo_evidence_fallback")) is False
    assert "repo_evidence_fallback" not in TRUSTED_LIVE_TRANSPORTS


def test_behavior_eval_passed_false_on_fallback_used() -> None:
    assert behavior_eval_passed(_live_report("hive_live", fallback=True)) is False


def test_behavior_eval_passed_false_when_incomplete() -> None:
    assert behavior_eval_passed(_live_report("hive_live", complete=False)) is False


def test_behavior_eval_passed_false_when_scenario_not_ready() -> None:
    assert behavior_eval_passed(_live_report("hive_live", ready=False)) is False


# ---- run_hive_behavior_eval (real orchestration + hard-grader scoring) ----


async def test_run_hive_behavior_eval_scores_with_hard_grader(tmp_path: Path) -> None:
    report = await run_hive_behavior_eval(agent_runner=_good_coding_runner, output_dir=tmp_path, scenarios=("coding",))
    assert report["transport"] == "hive_live"
    assert report["scenarios"]["coding"]["ready"] is True
    assert behavior_eval_passed(report) is True


async def test_run_hive_behavior_eval_failing_agent_not_ready(tmp_path: Path) -> None:
    report = await run_hive_behavior_eval(agent_runner=_bad_coding_runner, output_dir=tmp_path, scenarios=("coding",))
    assert report["scenarios"]["coding"]["ready"] is False
    assert behavior_eval_passed(report) is False


async def test_run_hive_behavior_eval_agent_error_is_fail_closed(tmp_path: Path) -> None:
    report = await run_hive_behavior_eval(agent_runner=_raising_runner, output_dir=tmp_path, scenarios=("coding",))
    assert report["transport"] == "hive_live_unavailable"
    assert report["benchmark_complete"] is False
    assert behavior_eval_passed(report) is False


# ---- record_behavior_eval_run (G1: behavior result -> ledger) ----


def test_record_behavior_eval_run_live_passed(tmp_path: Path) -> None:
    event = record_behavior_eval_run(tmp_path, candidate_id="cand-1", report=_live_report("hive_live"))
    assert event["passed"] is True
    assert 0.0 < float(event["reward"]) <= 1.0  # continuous, normalized


def test_record_behavior_eval_run_fallback_not_passed(tmp_path: Path) -> None:
    event = record_behavior_eval_run(tmp_path, candidate_id="cand-2", report=_live_report("repo_evidence_fallback"))
    assert event["passed"] is False


# ---- build_invoke_agent_runner (G0: real invoke_agent wiring, injectable) ----


async def test_build_invoke_agent_runner_constructs_request(tmp_path: Path) -> None:
    captured: dict = {}

    async def fake_invoke(request):
        captured["request"] = request
        from app.runtime.invoker import AgentInvocationResult

        return AgentInvocationResult(
            content='{"status":"success","answer":"ok","evidence":[],"files_created":[],"used_parallelism":false,"notes":""}'
        )

    runner = build_invoke_agent_runner(
        model="model-x", agent_name="eval-agent", role_description="evaluation agent", invoke=fake_invoke
    )
    payload = await runner("complete TASK.md", tmp_path)
    request = captured["request"]
    assert request.agent_name == "eval-agent"
    assert "complete TASK.md" in request.messages[-1]["content"]
    assert request.tool_executor is not None
    await request.tool_executor("write_file", {"path": "workspace/artifact.txt", "content": "ok"})
    assert (tmp_path / "workspace" / "artifact.txt").read_text(encoding="utf-8") == "ok"
    assert payload["status"] == "success"


async def test_build_invoke_agent_runner_normalizes_production_runtime_ids(tmp_path: Path) -> None:
    captured: dict = {}
    agent_id = uuid4()
    user_id = uuid4()

    async def fake_invoke(request):
        captured["request"] = request
        from app.runtime.invoker import AgentInvocationResult

        return AgentInvocationResult(content='{"status":"success"}')

    runner = build_invoke_agent_runner(
        model="model-x",
        agent_name="eval-agent",
        role_description="evaluation agent",
        agent_id=str(agent_id),
        user_id=str(user_id),
        invoke=fake_invoke,
    )
    await runner("complete TASK.md", tmp_path)

    assert captured["request"].agent_id == agent_id
    assert captured["request"].user_id == user_id


class _FakeScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeEvalSession:
    def __init__(self, rows):
        self._rows = list(rows)
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return _FakeScalarResult(self._rows.pop(0) if self._rows else None)


@asynccontextmanager
async def _noop_rls_bypass(_db, *, reason: str):
    assert "behavior eval" in reason
    yield


async def test_resolve_production_eval_runtime_uses_agent_tenant_model_pool() -> None:
    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    primary_model_id = uuid4()
    fallback_model_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        name="Eval Agent",
        role_description="Company-configured eval agent",
        primary_model_id=primary_model_id,
        fallback_model_id=fallback_model_id,
    )
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, is_active=True)
    primary = SimpleNamespace(
        id=primary_model_id,
        tenant_id=tenant_id,
        enabled=True,
        provider="anthropic",
        model="claude-opus-4-8",
        api_key="decrypted-from-db",
        base_url=None,
    )
    fallback = SimpleNamespace(
        id=fallback_model_id,
        tenant_id=tenant_id,
        enabled=True,
        provider="openai",
        model="gpt-5.5-pro",
        api_key="decrypted-from-db",
        base_url=None,
    )
    session = _FakeEvalSession([agent, user, primary, fallback])

    runtime = await resolve_production_eval_runtime(
        agent_id=agent_id,
        user_id=user_id,
        expected_tenant_id=tenant_id,
        session_factory=lambda: session,
        rls_bypass_factory=_noop_rls_bypass,
    )

    assert runtime.tenant_id == tenant_id
    assert runtime.agent_name == "Eval Agent"
    assert runtime.role_description == "Company-configured eval agent"
    assert runtime.model is primary
    assert runtime.fallback_model is fallback


async def test_resolve_production_eval_runtime_rejects_cross_tenant_model() -> None:
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    primary_model_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        name="Eval Agent",
        role_description="Company-configured eval agent",
        primary_model_id=primary_model_id,
        fallback_model_id=None,
    )
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, is_active=True)
    cross_tenant_model = SimpleNamespace(
        id=primary_model_id,
        tenant_id=other_tenant_id,
        enabled=True,
        provider="anthropic",
        model="claude-opus-4-8",
        api_key="wrong-tenant-key",
        base_url=None,
    )
    session = _FakeEvalSession([agent, user, cross_tenant_model])

    with pytest.raises(RuntimeError, match="tenant-scoped model"):
        await resolve_production_eval_runtime(
            agent_id=agent_id,
            user_id=user_id,
            expected_tenant_id=tenant_id,
            session_factory=lambda: session,
            rls_bypass_factory=_noop_rls_bypass,
        )


async def test_resolve_production_eval_runtime_uses_tenant_setting_when_ids_are_not_env() -> None:
    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    primary_model_id = uuid4()
    setting = {"agent_id": str(agent_id), "user_id": str(user_id)}
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        name="Eval Agent",
        role_description="Tenant setting eval agent",
        primary_model_id=primary_model_id,
        fallback_model_id=None,
    )
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, is_active=True)
    primary = SimpleNamespace(
        id=primary_model_id,
        tenant_id=tenant_id,
        enabled=True,
        provider="anthropic",
        model="claude-opus-4-8",
        api_key="decrypted-from-db",
        base_url=None,
    )
    session = _FakeEvalSession([setting, agent, user, primary])

    runtime = await resolve_production_eval_runtime(
        agent_id=None,
        user_id=None,
        expected_tenant_id=tenant_id,
        session_factory=lambda: session,
        rls_bypass_factory=_noop_rls_bypass,
    )

    assert runtime.agent_id == agent_id
    assert runtime.user_id == user_id
    assert runtime.model is primary


def test_production_runtime_cli_requires_eval_tenant(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--output", str(tmp_path / "report.json"), "--production-runtime"])

    assert exc.value.code == 2


def test_production_runtime_cli_uses_db_model_not_env_model(tmp_path: Path, monkeypatch) -> None:
    captured = {}
    tenant_id = uuid4()

    async def fake_run_cli(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr("app.evals.hive_live_runner._run_cli", fake_run_cli)
    monkeypatch.setenv("HIVE_EVAL_LLM_API_KEY", "should-not-be-read")

    code = main(
        [
            "--output",
            str(tmp_path / "report.json"),
            "--production-runtime",
            "--tenant-id",
            str(tenant_id),
        ]
    )

    assert code == 0
    assert captured["args"].model is None
    assert captured["args"].tenant_id == str(tenant_id)
    assert captured["args"].agent_id is None
    assert captured["args"].user_id is None


def test_write_behavior_report_stdout_marker(capsys) -> None:
    report = _live_report("hive_live")

    write_behavior_report(report, Path("-"))

    output = capsys.readouterr().out.strip()
    assert output.startswith(BEHAVIOR_REPORT_STDOUT_MARKER)
    encoded = output.removeprefix(BEHAVIOR_REPORT_STDOUT_MARKER)
    decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
    assert decoded == report


# ---- agent_behavior_check grader (into run_evolution_verification dispatch) ----


def test_agent_behavior_check_grader_trusted_passes(tmp_path: Path) -> None:
    report = run_evolution_verification(
        workspace=tmp_path,
        candidate={"candidate_id": "c1", "target_type": "skill"},
        graders=[{"type": "agent_behavior_check", "behavior_report": _live_report("hive_live")}],
    )
    assert report["passed"] is True


def test_agent_behavior_check_grader_fallback_fails(tmp_path: Path) -> None:
    report = run_evolution_verification(
        workspace=tmp_path,
        candidate={"candidate_id": "c1", "target_type": "skill"},
        graders=[{"type": "agent_behavior_check", "behavior_report": _live_report("repo_evidence_fallback")}],
    )
    assert report["passed"] is False
