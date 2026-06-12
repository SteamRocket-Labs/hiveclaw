from __future__ import annotations


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


def test_self_evolution_bakeoff_scores_hive_against_hermes_baseline() -> None:
    from app.evals.self_evolution_bakeoff import run_self_evolution_bakeoff

    report = run_self_evolution_bakeoff(
        hermes_scores={
            "next_turn_adaptation": 85,
            "repeated_workflow_learning": 84,
            "tool_failure_lesson_reuse": 80,
            "skill_candidate_creation": 78,
            "long_task_resume": 70,
            "safety_tenant_policy": 60,
        }
    )

    assert report["schema"] == "self_evolution_bakeoff.v1"
    assert report["passed"] is True
    assert report["hive"]["source"] == "local_behavior_scenarios"
    assert report["hive"]["behavior_complete"] is True
    next_turn = report["comparisons"]["next_turn_adaptation"]
    safety = report["comparisons"]["safety_tenant_policy"]
    assert next_turn["hive_score"] >= next_turn["hermes_score"]
    assert safety["hive_score"] > safety["hermes_score"]
    assert all("contains" not in check for check in next_turn["hive_evidence"])
    assert "Session Learning" in report["hive"]["scenarios"]["next_turn_adaptation"]["transcript"]
    assert report["cost_latency"]["visible"] is True
    assert report["cost_latency"]["bounded"] is True


def test_self_evolution_bakeoff_reports_failure_when_hive_loses_next_turn() -> None:
    from app.evals.self_evolution_bakeoff import run_self_evolution_bakeoff

    report = run_self_evolution_bakeoff(
        hermes_scores={
            "next_turn_adaptation": 100,
            "repeated_workflow_learning": 1,
            "tool_failure_lesson_reuse": 1,
            "skill_candidate_creation": 1,
            "long_task_resume": 1,
            "safety_tenant_policy": 1,
        }
    )

    assert report["passed"] is False
    assert "next_turn_adaptation" in report["failed_requirements"]
