"""E8: the CI behavior-eval gate decides exit code from E1/E2/E5 results (red first).

per-PR blocks merge on regression or a required-live fallback (decision D2/D3).
"""

from __future__ import annotations

from app.evals.ci_gate import (
    EXIT_BASELINE_UNAVAILABLE,
    EXIT_OK,
    EXIT_REGRESSION,
    EXIT_REQUIRED_LIVE_FALLBACK,
    EXIT_UNTRUSTED_EVALUATOR,
    evaluate_ci_gate,
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
