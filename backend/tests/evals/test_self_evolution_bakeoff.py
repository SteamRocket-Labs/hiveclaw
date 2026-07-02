from __future__ import annotations

import json

import pytest


def test_self_evolution_bakeoff_dataset_covers_foundation_scenarios() -> None:
    from app.evals.self_evolution_bakeoff import build_self_evolution_bakeoff_dataset

    dataset = build_self_evolution_bakeoff_dataset()
    names = {case["name"] for case in dataset}

    assert names == {
        "next_turn_adaptation",
        "repeated_workflow_learning",
        "tool_failure_lesson_reuse",
        "skill_candidate_creation",
        "long_task_resume",
        "safety_tenant_policy",
    }
    assert all(case["prompt"] for case in dataset)
    assert all(case["behavior_assertions"] for case in dataset)
    assert all("deterministic_checks" not in case for case in dataset)


def test_self_evolution_bakeoff_uses_hive_absolute_gate_when_hermes_unavailable() -> None:
    from app.evals.self_evolution_bakeoff import run_self_evolution_bakeoff

    report = run_self_evolution_bakeoff()

    assert report["schema"] == "self_evolution_bakeoff.v1"
    assert report["passed"] is True
    assert report["hive"]["source"] == "local_behavior_scenarios"
    assert report["hive"]["behavior_complete"] is True
    next_turn = report["comparisons"]["next_turn_adaptation"]
    safety = report["comparisons"]["safety_tenant_policy"]
    assert report["hermes"]["source"] == "unavailable"
    assert report["hermes"]["scores"] == {}
    assert next_turn["hermes_score"] is None
    assert safety["hermes_score"] is None
    assert all("contains" not in check for check in next_turn["hive_evidence"])
    assert "Session Learning" in report["hive"]["scenarios"]["next_turn_adaptation"]["transcript"]
    long_task_transcript = json.loads(report["hive"]["scenarios"]["long_task_resume"]["transcript"])
    work_ledger = long_task_transcript["work_ledger"]
    assert work_ledger["open_required_todos"] == [
        "Report includes canary status.",
        "Report includes rollback checklist.",
    ]
    assert work_ledger["verification_pending"] == ["pytest tests/services/test_long_task_runtime.py"]
    assert report["cost_latency"]["visible"] is True
    assert report["cost_latency"]["bounded"] is True


def test_self_evolution_bakeoff_rejects_injected_hermes_scores() -> None:
    from app.evals.self_evolution_bakeoff import run_self_evolution_bakeoff

    with pytest.raises(ValueError, match="Hermes scores must come from a live run"):
        run_self_evolution_bakeoff(hermes_scores={"next_turn_adaptation": 100})


def test_self_evolution_bakeoff_cli_rejects_injected_hermes_scores() -> None:
    from app.evals.self_evolution_bakeoff import main

    with pytest.raises(SystemExit) as exc:
        main(["--hermes-scores-json", "{}"])

    assert exc.value.code == 2
