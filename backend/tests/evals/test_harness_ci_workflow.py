from __future__ import annotations

from pathlib import Path


def test_harness_ci_runs_pytest_prompt_eval_and_self_evolution_bakeoff() -> None:
    workflow = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "harness-ci.yml"

    assert workflow.exists()
    source = workflow.read_text(encoding="utf-8")
    assert "pytest" in source
    assert "python -m app.memory.retrieval_eval" in source
    assert "python -m app.runtime.prompt_eval" in source
    assert "python -m app.evals.self_evolution_bakeoff" in source
    assert "python -m app.evals.run --suite core_v1 --target clawith --mode internal" in source


def test_nightly_behavior_gate_generates_live_report_and_integrity_before_gate() -> None:
    workflow = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "harness-ci.yml"
    source = workflow.read_text(encoding="utf-8")

    live_runner = "python -m app.evals.hive_live_runner"
    integrity = "python -m app.evals.evaluator_integrity"
    gate = "python -m app.evals.ci_gate"

    assert live_runner in source
    assert '--output "$HIVE_EVAL_REPORT"' in source
    assert integrity in source
    assert '--output "$HIVE_EVAL_INTEGRITY"' in source
    assert '--integrity-report "$HIVE_EVAL_INTEGRITY"' in source
    assert source.index(live_runner) < source.index(gate)
    assert source.index(integrity) < source.index(gate)
