from __future__ import annotations

import json


def test_missing_eval_secrets_produce_blocked_evidence(tmp_path) -> None:
    from app.evals.behavior_eval_evidence import build_missing_secret_evidence, write_behavior_eval_evidence

    evidence = build_missing_secret_evidence(api_url="", token="")

    assert evidence["schema"] == "behavior_eval_evidence.v1"
    assert evidence["status"] == "blocked"
    assert evidence["passed"] is False
    assert "HIVE_EVAL_API_URL" in evidence["missing"]
    assert "HIVE_EVAL_CI_TOKEN" in evidence["missing"]

    output = tmp_path / "behavior-eval-evidence.json"
    write_behavior_eval_evidence(evidence, output)
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "blocked"


def test_harness_workflow_does_not_silently_skip_missing_live_eval_secrets() -> None:
    from pathlib import Path

    workflow = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "harness-ci.yml"
    source = workflow.read_text(encoding="utf-8")

    assert "python -m app.evals.behavior_eval_evidence" in source
    assert "behavior-eval-evidence" in source
    assert "behavior eval gate skipped" not in source
    assert "exit 0" not in source[source.index("Behavior eval gate (live; runs when eval secrets configured)") :]
