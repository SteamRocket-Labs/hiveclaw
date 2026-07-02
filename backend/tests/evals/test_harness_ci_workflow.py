from __future__ import annotations

from pathlib import Path


def test_harness_ci_runs_pytest_prompt_eval_and_self_evolution_bakeoff() -> None:
    workflow = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "harness-ci.yml"

    assert workflow.exists()
    source = workflow.read_text(encoding="utf-8")
    assert "pytest" in source
    # retrieval_eval retired at the C7 two-plane cutover; wiki retrieval is
    # covered by tests/memory suites, so CI must not invoke the dead module.
    assert "python -m app.memory.retrieval_eval" not in source
    assert "python -m app.runtime.prompt_eval" in source
    assert "python -m app.evals.self_evolution_bakeoff" in source
    assert "python -m app.evals.run --suite core_v1 --target clawith --mode internal" in source
    assert "python -m app.evals.adversarial_suite" in source


def test_nightly_behavior_gate_generates_live_report_and_integrity_before_gate() -> None:
    workflow = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "harness-ci.yml"
    source = workflow.read_text(encoding="utf-8")

    live_endpoint = "/api/eval-ci/behavior"
    integrity = "python -m app.evals.evaluator_integrity"
    gate = "python -m app.evals.ci_gate"

    assert "workflow_dispatch:" in source
    assert "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'" in source
    assert "railway ssh" not in source
    assert "RAILWAY_TOKEN" not in source
    assert "HIVE_EVAL_API_URL" in source
    assert "HIVE_EVAL_CI_TOKEN" in source
    assert "HIVE_EVAL_LLM_API_KEY" not in source
    assert "HIVE_EVAL_MODEL:" not in source
    assert "--model ${HIVE_EVAL_MODEL" not in source
    assert "curl -fsSL https://railway.app/install.sh" not in source
    assert "ssh-keygen -t ed25519" not in source
    assert live_endpoint in source
    assert "curl -sS" in source
    assert 'cat "$HIVE_EVAL_REPORT"' in source
    assert '"$HIVE_EVAL_REPORT"' in source
    assert '--running-model "$RUNNING_MODEL"' in source
    assert integrity in source
    assert '--output "$HIVE_EVAL_INTEGRITY"' in source
    assert '--integrity-report "$HIVE_EVAL_INTEGRITY"' in source
    assert '--adversarial-report "$HIVE_EVAL_ADVERSARIAL"' in source
    assert source.index(integrity) < source.index(live_endpoint)
    assert source.index(live_endpoint) < source.index(gate)
    assert source.index(integrity) < source.index(gate)
