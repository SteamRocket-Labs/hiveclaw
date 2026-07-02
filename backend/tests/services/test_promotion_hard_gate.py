"""Promotion hard gate: hard verification floors enter provisional trial."""

from __future__ import annotations

from pathlib import Path

from app.services.evolution_verification import (
    decide_provisional_promotion,
    decide_verified_promotion,
    execution_evidence,
)


def _verification(passed: bool, *, behavior: bool | None = None) -> dict:
    checks = [{"type": "skill_guard", "passed": passed}]
    if behavior is not None:
        checks.append({"type": "agent_behavior_check", "passed": behavior})
    return {"passed": passed and (behavior is not False), "checks": checks}


def _regression(passed: bool) -> dict:
    return {
        "suite": "core_behavior_v1",
        "passed": passed,
        "regressions": [] if passed else [{"scenario": "coding", "regressed": True}],
        "missing_scenarios": [],
    }


def _artifact_report(status: str, *, passed: bool | None = None, reason: str = "artifact gate") -> dict:
    payload = {"status": status, "reason": reason}
    if passed is not None:
        payload["passed"] = passed
    return payload


# ---- decide_verified_promotion: regression gate (backward-compatible) ----


def test_decide_verified_promotion_unchanged_without_regression_report() -> None:
    assert (
        decide_verified_promotion({"candidate_id": "c"}, verification_report=_verification(True))["decision"]
        == "promote"
    )
    assert (
        decide_verified_promotion({"candidate_id": "c"}, verification_report=_verification(False))["decision"]
        == "reject"
    )
    assert decide_verified_promotion({"candidate_id": "c"}, verification_report=None)["decision"] == "hold"


def test_decide_verified_promotion_holds_on_regression() -> None:
    decision = decide_verified_promotion(
        {"candidate_id": "c"},
        verification_report=_verification(True),
        regression_report=_regression(False),
    )
    assert decision["decision"] == "hold"
    assert "regress" in decision["reason"].lower()


def test_decide_verified_promotion_promotes_without_regression() -> None:
    decision = decide_verified_promotion(
        {"candidate_id": "c"},
        verification_report=_verification(True),
        regression_report=_regression(True),
    )
    assert decision["decision"] == "promote"


# ---- execution_evidence: surface behavior-check presence/result ----


def test_execution_evidence_detects_passing_behavior_check() -> None:
    evidence = execution_evidence(_verification(True, behavior=True))
    assert evidence["has_behavior_check"] is True
    assert evidence["execution_passed"] is True


def test_execution_evidence_detects_failing_behavior_check() -> None:
    evidence = execution_evidence(_verification(True, behavior=False))
    assert evidence["has_behavior_check"] is True
    assert evidence["execution_passed"] is False


def test_execution_evidence_absent_behavior_check() -> None:
    evidence = execution_evidence(_verification(True))
    assert evidence["has_behavior_check"] is False
    assert evidence["execution_passed"] is False


# ---- decide_provisional_promotion: hard floors enter trial; behavior eval is retired ----


def test_provisional_gate_rejects_on_verification_fail() -> None:
    decision = decide_provisional_promotion(
        {"candidate_id": "c"},
        verification_report=_verification(False),
    )
    assert decision["decision"] == "reject"


def test_provisional_gate_enters_trial_without_behavior_or_regression_report() -> None:
    decision = decide_provisional_promotion(
        {"candidate_id": "c"},
        verification_report=_verification(True),
    )
    assert decision["decision"] == "provisional"
    assert "trial" in decision["reason"].lower()


def test_provisional_gate_ignores_legacy_regression_report_when_hard_floor_passes() -> None:
    decision = decide_provisional_promotion(
        {"candidate_id": "c"},
        verification_report=_verification(True),
        regression_report=_regression(False),
    )
    assert decision["decision"] == "provisional"


def test_provisional_gate_holds_skill_candidate_without_artifact_gate_report() -> None:
    decision = decide_provisional_promotion(
        {"candidate_id": "c", "target_type": "skill"},
        verification_report=_verification(True),
    )
    assert decision["decision"] == "hold"
    assert "artifact" in decision["reason"].lower()


def test_provisional_gate_holds_skill_candidate_on_failed_artifact_gate() -> None:
    decision = decide_provisional_promotion(
        {"candidate_id": "c", "target_type": "skill_patch"},
        verification_report=_verification(True),
        artifact_gate_report=_artifact_report("failed", passed=False, reason="verification script failed"),
    )
    assert decision["decision"] == "hold"
    assert "verification script failed" in decision["reason"]


def test_provisional_gate_enters_trial_for_skill_candidate_with_passed_artifact_gate() -> None:
    decision = decide_provisional_promotion(
        {"candidate_id": "c", "target_type": "skill"},
        verification_report=_verification(True),
        artifact_gate_report=_artifact_report("passed", passed=True),
    )
    assert decision["decision"] == "provisional"


def test_skill_distiller_real_promotion_paths_use_provisional_gate() -> None:
    source = (Path(__file__).resolve().parents[2] / "app" / "services" / "skill_distiller.py").read_text(
        encoding="utf-8"
    )

    assert "decide_provisional_promotion" in source
    assert "decide_behavior_gated_promotion" not in source
    assert "decide_verified_promotion(candidate, verification_report=verification_report)" not in source
    assert "artifact_gate_report=" in source
    assert "ensure_skill_distiller_behavior_report" not in source
