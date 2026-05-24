from __future__ import annotations

import sys


def test_evolution_verification_promotes_passing_deterministic_command(tmp_path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger, record_evolution_candidate
    from app.services.evolution_verification import (
        decide_verified_promotion,
        record_verification_eval,
        run_evolution_verification,
    )

    candidate = record_evolution_candidate(
        tmp_path,
        target_type="fast_reflection",
        target_id="session-1:user_preference_correction",
        diff="+ candidate",
        source_attempt_ids=["session-1"],
        baseline_version="candidate",
    )
    report = run_evolution_verification(
        workspace=tmp_path,
        candidate=candidate,
        graders=[
            {
                "type": "deterministic_command",
                "command": [sys.executable, "-c", "print('ok')"],
            }
        ],
    )
    eval_run = record_verification_eval(tmp_path, candidate=candidate, verification_report=report)
    decision = decide_verified_promotion(candidate, verification_report=report)

    entries = load_evolution_ledger(tmp_path)
    assert report["schema"] == "evolution_verification_report.v1"
    assert report["passed"] is True
    assert eval_run["schema"] == "evolution_eval_run.v1"
    assert eval_run["passed"] is True
    assert decision["decision"] == "promote"
    assert entries[-1]["event"] == "eval_run"


def test_evolution_verification_rejects_failed_command(tmp_path) -> None:
    from app.services.evolution_ledger import record_evolution_candidate
    from app.services.evolution_verification import decide_verified_promotion, run_evolution_verification

    candidate = record_evolution_candidate(
        tmp_path,
        target_type="fast_reflection",
        target_id="session-1:verification_failure",
        diff="+ candidate",
        source_attempt_ids=["session-1"],
        baseline_version="candidate",
    )
    report = run_evolution_verification(
        workspace=tmp_path,
        candidate=candidate,
        graders=[
            {
                "type": "deterministic_command",
                "command": [sys.executable, "-c", "raise SystemExit(2)"],
            }
        ],
    )

    decision = decide_verified_promotion(candidate, verification_report=report)

    assert report["passed"] is False
    assert decision["decision"] == "reject"
    assert "verification failed" in decision["reason"]


def test_evolution_verification_holds_without_verification(tmp_path) -> None:
    from app.services.evolution_ledger import record_evolution_candidate
    from app.services.evolution_verification import decide_verified_promotion

    candidate = record_evolution_candidate(
        tmp_path,
        target_type="fast_reflection",
        target_id="session-1:user_preference_correction",
        diff="+ candidate",
        source_attempt_ids=["session-1"],
        baseline_version="candidate",
    )

    decision = decide_verified_promotion(candidate, verification_report=None)

    assert decision == {"decision": "hold", "reason": "verification evidence is required"}


def test_evolution_verification_supports_state_and_human_checks(tmp_path) -> None:
    from app.services.evolution_ledger import record_evolution_candidate
    from app.services.evolution_verification import run_evolution_verification

    (tmp_path / "artifact.txt").write_text("ready", encoding="utf-8")
    candidate = record_evolution_candidate(
        tmp_path,
        target_type="fast_reflection",
        target_id="session-1:workflow_correction",
        diff="+ candidate",
        source_attempt_ids=["session-1"],
        baseline_version="candidate",
    )
    report = run_evolution_verification(
        workspace=tmp_path,
        candidate=candidate,
        graders=[
            {"type": "state_check", "path": "artifact.txt", "contains": "ready"},
            {"type": "human_confirmation", "confirmed": True, "reviewer": "owner"},
        ],
    )

    assert report["passed"] is True
    assert {check["type"] for check in report["checks"]} == {"state_check", "human_confirmation"}
