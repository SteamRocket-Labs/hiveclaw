"""E8: the CI behavior-eval gate decides exit code from E1/E2/E5 results (red first).

per-PR blocks merge on regression or a required-live fallback (decision D2/D3).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.evals.ci_gate import (
    EXIT_ARTIFACT_GATE_FAILED,
    EXIT_BASELINE_UNAVAILABLE,
    EXIT_OK,
    EXIT_REGRESSION,
    EXIT_REQUIRED_LIVE_FALLBACK,
    EXIT_UNTRUSTED_EVALUATOR,
    evaluate_ci_gate,
    main,
)


def _baseline(scores: dict[str, float], *, model: str = "claude-opus-4-8") -> dict:
    return {
        "schema": "behavior_eval_baseline.v1",
        "suite": "core_behavior_v1",
        "baseline_version": "1.0.0",
        "baseline_model": model,
        "baseline_date": "2026-06-13",
        "commit_sha": "abc",
        "scenarios": {name: {"score_p50": value} for name, value in scores.items()},
    }


def _report(
    scores: dict[str, float], *, transport: str = "hive_live", complete: bool = True, fallback: bool = False
) -> dict:
    return {
        "transport": transport,
        "benchmark_complete": complete,
        "fallback_used": fallback,
        "scenarios": {name: {"ready": True, "score": value} for name, value in scores.items()},
    }


def test_gate_passes_with_no_regression() -> None:
    decision = evaluate_ci_gate(
        behavior_report=_report({"coding": 100}),
        baseline=_baseline({"coding": 100}),
        running_model="claude-opus-4-8",
    )
    assert decision.passed is True
    assert decision.exit_code == EXIT_OK


def test_gate_fails_on_regression() -> None:
    decision = evaluate_ci_gate(
        behavior_report=_report({"coding": 70}),
        baseline=_baseline({"coding": 100}),
        running_model="claude-opus-4-8",
    )
    assert decision.passed is False
    assert decision.exit_code == EXIT_REGRESSION


def test_gate_fails_on_required_live_fallback() -> None:
    decision = evaluate_ci_gate(
        behavior_report=_report({"coding": 100}, transport="repo_evidence_fallback"),
        baseline=_baseline({"coding": 100}),
        running_model="claude-opus-4-8",
        require_live=True,
    )
    assert decision.passed is False
    assert decision.exit_code == EXIT_REQUIRED_LIVE_FALLBACK


def test_gate_fails_on_untrusted_evaluator() -> None:
    decision = evaluate_ci_gate(
        behavior_report=_report({"coding": 100}),
        baseline=_baseline({"coding": 100}),
        running_model="claude-opus-4-8",
        integrity={"trusted": False},
    )
    assert decision.passed is False
    assert decision.exit_code == EXIT_UNTRUSTED_EVALUATOR


def test_gate_fails_when_required_artifact_gate_report_missing() -> None:
    decision = evaluate_ci_gate(
        behavior_report=_report({"coding": 100}),
        baseline=_baseline({"coding": 100}),
        running_model="claude-opus-4-8",
        require_artifact_gate=True,
    )
    assert decision.passed is False
    assert decision.exit_code == EXIT_ARTIFACT_GATE_FAILED
    assert "artifact" in decision.reasons[0].lower()


def test_gate_fails_when_adversarial_suite_does_not_block_all_attacks() -> None:
    decision = evaluate_ci_gate(
        behavior_report=_report({"coding": 100}),
        baseline=_baseline({"coding": 100}),
        running_model="claude-opus-4-8",
        adversarial_report={"all_blocked": False, "attacks": {"fake_pass_claim": False}},
    )
    assert decision.passed is False
    assert decision.exit_code == EXIT_ARTIFACT_GATE_FAILED
    assert "adversarial" in decision.reasons[0].lower()


def test_gate_fails_on_baseline_unavailable() -> None:
    decision = evaluate_ci_gate(
        behavior_report=_report({"coding": 100}),
        baseline=None,
        running_model="claude-opus-4-8",
    )
    assert decision.passed is False
    assert decision.exit_code == EXIT_BASELINE_UNAVAILABLE


def test_gate_fails_on_model_drift() -> None:
    decision = evaluate_ci_gate(
        behavior_report=_report({"coding": 100}),
        baseline=_baseline({"coding": 100}, model="claude-opus-4-8"),
        running_model="claude-sonnet-4-6",
    )
    assert decision.passed is False


def test_gate_tolerance_allows_small_dip() -> None:
    decision = evaluate_ci_gate(
        behavior_report=_report({"coding": 99}),
        baseline=_baseline({"coding": 100}),
        running_model="claude-opus-4-8",
        tolerance=2.0,
    )
    assert decision.passed is True


def test_cli_fails_on_integrity_report_untrusted(tmp_path: Path) -> None:
    behavior_report = tmp_path / "behavior.json"
    baseline = tmp_path / "core_behavior_v1.json"
    integrity_report = tmp_path / "integrity.json"
    behavior_report.write_text(json.dumps(_report({"coding": 100})), encoding="utf-8")
    baseline.write_text(json.dumps(_baseline({"coding": 100})), encoding="utf-8")
    integrity_report.write_text(
        json.dumps({"trusted": False, "reason": "evaluator modified without protected review"}),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--behavior-report",
            str(behavior_report),
            "--baseline",
            str(baseline),
            "--running-model",
            "claude-opus-4-8",
            "--integrity-report",
            str(integrity_report),
        ]
    )

    assert exit_code == EXIT_UNTRUSTED_EVALUATOR


def test_cli_fails_on_required_artifact_report_failed(tmp_path: Path) -> None:
    behavior_report = tmp_path / "behavior.json"
    baseline = tmp_path / "core_behavior_v1.json"
    artifact_report = tmp_path / "artifact.json"
    behavior_report.write_text(json.dumps(_report({"coding": 100})), encoding="utf-8")
    baseline.write_text(json.dumps(_baseline({"coding": 100})), encoding="utf-8")
    artifact_report.write_text(json.dumps({"status": "failed", "passed": False, "reason": "exit 1"}), encoding="utf-8")

    exit_code = main(
        [
            "--behavior-report",
            str(behavior_report),
            "--baseline",
            str(baseline),
            "--running-model",
            "claude-opus-4-8",
            "--artifact-report",
            str(artifact_report),
            "--require-artifact-gate",
        ]
    )

    assert exit_code == EXIT_ARTIFACT_GATE_FAILED
