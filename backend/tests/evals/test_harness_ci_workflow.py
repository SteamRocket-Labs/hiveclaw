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
