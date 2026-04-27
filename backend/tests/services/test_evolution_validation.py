from __future__ import annotations


def test_validate_evolution_ledger_passes_complete_promotion_chain(tmp_path):
    from app.services.evolution_ledger import (
        decide_promotion,
        record_eval_run,
        record_evolution_candidate,
        record_promotion_decision,
    )
    from app.services.evolution_validation import validate_evolution_ledger

    workspace = tmp_path / "agent"
    candidate = record_evolution_candidate(
        workspace,
        target_type="skill",
        target_id="market-research-loop",
        diff="+ reliable workflow",
        source_attempt_ids=["rt-1", "rt-2"],
        baseline_version="v1",
    )
    eval_run = record_eval_run(
        workspace,
        candidate_id=candidate["candidate_id"],
        dataset="skill_distiller.internal",
        reward=0.91,
        baseline_reward=0.80,
        passed=True,
        traces=["rt-1", "rt-2"],
        critical_regressions=0,
    )
    decision = decide_promotion(eval_run, min_reward_delta=0.05)
    record_promotion_decision(
        workspace,
        candidate_id=candidate["candidate_id"],
        decision=decision["decision"],
        reason=decision["reason"],
        rollback_ref="skills/market-research-loop/SKILL.md@v1",
    )

    report = validate_evolution_ledger(workspace, write_report=True)

    assert report["schema"] == "evolution_validation_report.v1"
    assert report["passed"] is True
    assert report["summary"]["fail"] == 0
    assert report["report_artifact"]["path"] == "evolution/evolution_validation_report.json"
    assert (workspace / "evolution" / "evolution_validation_report.json").exists()


def test_validate_evolution_ledger_flags_promotion_without_eval_or_rollback(tmp_path):
    from app.services.evolution_ledger import record_evolution_candidate, record_promotion_decision
    from app.services.evolution_validation import validate_evolution_ledger

    workspace = tmp_path / "agent"
    candidate = record_evolution_candidate(
        workspace,
        target_type="skill",
        target_id="unsafe-skill",
        diff="+ unvalidated behavior",
        source_attempt_ids=[],
        baseline_version=None,
    )
    record_promotion_decision(
        workspace,
        candidate_id=candidate["candidate_id"],
        decision="promote",
        reason="manual shortcut",
        rollback_ref=None,
    )

    report = validate_evolution_ledger(workspace)

    failed_ids = {check["id"] for check in report["checks"] if check["status"] == "fail"}
    assert report["passed"] is False
    assert "candidate_has_source_attempts" in failed_ids
    assert "candidate_has_eval_run" in failed_ids
    assert "promotion_has_rollback_ref" in failed_ids


def test_validate_evolution_ledger_blocks_promoted_critical_regression(tmp_path):
    from app.services.evolution_ledger import record_eval_run, record_evolution_candidate, record_promotion_decision
    from app.services.evolution_validation import validate_evolution_ledger

    workspace = tmp_path / "agent"
    candidate = record_evolution_candidate(
        workspace,
        target_type="prompt",
        target_id="heartbeat",
        diff="+ risky prompt",
        source_attempt_ids=["rt-1"],
        baseline_version="v1",
    )
    record_eval_run(
        workspace,
        candidate_id=candidate["candidate_id"],
        dataset="heartbeat.eval",
        reward=0.99,
        baseline_reward=0.50,
        passed=True,
        traces=["rt-1"],
        critical_regressions=1,
    )
    record_promotion_decision(
        workspace,
        candidate_id=candidate["candidate_id"],
        decision="promote",
        reason="bad shortcut",
        rollback_ref="prompts/heartbeat@v1",
    )

    report = validate_evolution_ledger(workspace)

    failed_ids = {check["id"] for check in report["checks"] if check["status"] == "fail"}
    assert "promotion_blocks_critical_regression" in failed_ids


def test_record_rollback_event_appends_auditable_rollback(tmp_path):
    from app.services.evolution_ledger import load_evolution_ledger, record_rollback_event

    workspace = tmp_path / "agent"
    rollback = record_rollback_event(
        workspace,
        candidate_id="cand-1",
        restored_ref="skills/old/SKILL.md@v1",
        reason="eval regression after promotion",
        operator="system",
    )

    entries = load_evolution_ledger(workspace)
    assert rollback["schema"] == "evolution_rollback_event.v1"
    assert rollback["event"] == "rollback"
    assert entries[-1]["restored_ref"] == "skills/old/SKILL.md@v1"
