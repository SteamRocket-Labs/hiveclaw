from __future__ import annotations


def test_evolution_ledger_records_candidate_eval_and_promotion(tmp_path):
    from app.services.evolution_ledger import (
        decide_promotion,
        load_evolution_ledger,
        record_eval_run,
        record_evolution_candidate,
        record_promotion_decision,
    )

    workspace = tmp_path / "agent"
    candidate = record_evolution_candidate(
        workspace,
        target_type="skill",
        target_id="market-research-loop",
        diff="+ new skill body",
        source_attempt_ids=["rt-1", "rt-2"],
        baseline_version="none",
        metadata={"workflow_signature": "web_search>web_fetch>write_file"},
    )
    eval_run = record_eval_run(
        workspace,
        candidate_id=candidate["candidate_id"],
        dataset="skill_distiller.internal",
        reward=0.92,
        baseline_reward=0.80,
        passed=True,
        traces=["s-1", "s-2"],
        critical_regressions=0,
    )
    decision = decide_promotion(eval_run, min_reward_delta=0.05)
    promotion = record_promotion_decision(
        workspace,
        candidate_id=candidate["candidate_id"],
        decision=decision["decision"],
        reason=decision["reason"],
        rollback_ref="skills/market-research-loop/SKILL.md",
    )

    entries = load_evolution_ledger(workspace)
    assert candidate["schema"] == "evolution_candidate.v1"
    assert eval_run["schema"] == "evolution_eval_run.v1"
    assert promotion["schema"] == "evolution_promotion_decision.v1"
    assert decision["decision"] == "promote"
    assert [entry["event"] for entry in entries] == ["candidate", "eval_run", "promotion_decision"]
    assert entries[0]["candidate_id"] == candidate["candidate_id"]


def test_evolution_promotion_policy_blocks_critical_regression():
    from app.services.evolution_ledger import decide_promotion

    decision = decide_promotion(
        {
            "reward": 0.99,
            "baseline_reward": 0.50,
            "passed": True,
            "critical_regressions": 1,
        }
    )

    assert decision["decision"] == "hold"
    assert "critical regression" in decision["reason"]
