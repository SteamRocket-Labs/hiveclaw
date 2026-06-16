"""E3: promotion hard gate — execution_passed AND no_regressions (red first)."""

from __future__ import annotations

from pathlib import Path

from app.services.evolution_verification import (
    decide_behavior_gated_promotion,
    decide_verified_promotion,
    execution_evidence,
)


def _verification(passed: bool, *, behavior: bool | None = None) -> dict:
    checks = [{"type": "skill_guard", "passed": passed}]
    if behavior is not None:
        checks.append({"type": "agent_behavior_check", "passed": behavior})
    return {"passed": passed and (behavior is not False), "checks": checks}


def _behavior_report(passed: bool) -> dict:
    return {
        "kind": "behavior_eval",
        "transport": "hive_live" if passed else "repo_evidence_fallback",
        "benchmark_complete": True,
        "fallback_used": False,
        "scenarios": {"coding": {"ready": passed, "score": 100 if passed else 0}},
    }


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


# ---- decide_behavior_gated_promotion: the canonical hard gate (E8 CI path) ----


def test_behavior_gated_rejects_on_verification_fail() -> None:
    decision = decide_behavior_gated_promotion(
        {"candidate_id": "c"},
        verification_report=_verification(False),
        behavior_report=_behavior_report(True),
    )
    assert decision["decision"] == "reject"


def test_behavior_gated_holds_on_behavior_fail() -> None:
    decision = decide_behavior_gated_promotion(
        {"candidate_id": "c"},
        verification_report=_verification(True),
        behavior_report=_behavior_report(False),
    )
    assert decision["decision"] == "hold"


def test_behavior_gated_holds_on_regression() -> None:
    decision = decide_behavior_gated_promotion(
        {"candidate_id": "c"},
        verification_report=_verification(True),
        behavior_report=_behavior_report(True),
        regression_report=_regression(False),
    )
    assert decision["decision"] == "hold"


def test_behavior_gated_promotes_when_all_pass() -> None:
    decision = decide_behavior_gated_promotion(
        {"candidate_id": "c"},
        verification_report=_verification(True),
        behavior_report=_behavior_report(True),
        regression_report=_regression(True),
    )
    assert decision["decision"] == "promote"


def test_behavior_gated_holds_skill_candidate_without_artifact_gate_report() -> None:
    decision = decide_behavior_gated_promotion(
        {"candidate_id": "c", "target_type": "skill"},
        verification_report=_verification(True),
        behavior_report=_behavior_report(True),
        regression_report=_regression(True),
    )
    assert decision["decision"] == "hold"
    assert "artifact" in decision["reason"].lower()


def test_behavior_gated_holds_skill_candidate_on_failed_artifact_gate() -> None:
    decision = decide_behavior_gated_promotion(
        {"candidate_id": "c", "target_type": "skill_patch"},
        verification_report=_verification(True),
        behavior_report=_behavior_report(True),
        regression_report=_regression(True),
        artifact_gate_report=_artifact_report("failed", passed=False, reason="verification script failed"),
    )
    assert decision["decision"] == "hold"
    assert "verification script failed" in decision["reason"]


def test_behavior_gated_promotes_skill_candidate_with_passed_artifact_gate() -> None:
    decision = decide_behavior_gated_promotion(
        {"candidate_id": "c", "target_type": "skill"},
        verification_report=_verification(True),
        behavior_report=_behavior_report(True),
        regression_report=_regression(True),
        artifact_gate_report=_artifact_report("passed", passed=True),
    )
    assert decision["decision"] == "promote"


def test_behavior_gated_holds_behavior_candidate_without_regression_report() -> None:
    decision = decide_behavior_gated_promotion(
        {"candidate_id": "c", "target_type": "skill"},
        verification_report=_verification(True),
        behavior_report=_behavior_report(True),
        artifact_gate_report=_artifact_report("passed", passed=True),
    )
    assert decision["decision"] == "hold"
    assert "regression" in decision["reason"].lower()


def test_behavior_gated_holds_on_missing_behavior_report() -> None:
    decision = decide_behavior_gated_promotion(
        {"candidate_id": "c"},
        verification_report=_verification(True),
        behavior_report=None,
    )
    assert decision["decision"] == "hold"


def test_skill_distiller_real_promotion_paths_use_behavior_gate() -> None:
    source = (Path(__file__).resolve().parents[2] / "app" / "services" / "skill_distiller.py").read_text(
        encoding="utf-8"
    )

    assert "decide_behavior_gated_promotion" in source
    assert "decide_verified_promotion(candidate, verification_report=verification_report)" not in source
    assert "artifact_gate_report=" in source
