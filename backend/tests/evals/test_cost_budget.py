"""E10: per-scenario cost/latency budget enforcement (red first)."""

from __future__ import annotations

from app.evals.cost_budget import (
    check_cost_budget,
    extract_scenario_costs,
    format_budget_warnings,
)


def test_within_budget_has_no_violations() -> None:
    report = check_cost_budget({"coding": {"duration_ms": 1000.0, "tokens": 5000.0}})
    assert report["within_budget"] is True
    assert report["violations"] == []


def test_over_duration_is_flagged() -> None:
    report = check_cost_budget(
        {"slow": {"duration_ms": 200_000.0, "tokens": 0.0}},
        per_scenario_budget={"max_duration_ms": 120_000.0, "max_tokens": 200_000.0},
    )
    assert report["within_budget"] is False
    assert report["violations"][0]["scenario"] == "slow"
    assert report["violations"][0]["metric"] == "duration_ms"


def test_over_tokens_is_flagged() -> None:
    report = check_cost_budget(
        {"chatty": {"duration_ms": 1000.0, "tokens": 500_000.0}},
        per_scenario_budget={"max_duration_ms": 120_000.0, "max_tokens": 200_000.0},
    )
    assert report["within_budget"] is False
    assert report["violations"][0]["metric"] == "tokens"


def test_zero_tokens_not_measured_never_violates() -> None:
    # Token telemetry absent (0) must not raise a false token violation.
    report = check_cost_budget({"coding": {"duration_ms": 1000.0, "tokens": 0.0}})
    assert report["within_budget"] is True


def test_extract_scenario_costs_from_behavior_report() -> None:
    behavior_report = {
        "scenarios": {
            "coding": {"ready": True, "score": 100, "score_breakdown": {"duration_ms": 4200, "tokens_used": 12000}},
        }
    }
    costs = extract_scenario_costs(behavior_report)
    assert costs["coding"]["duration_ms"] == 4200.0
    assert costs["coding"]["tokens"] == 12000.0


def test_format_budget_warnings() -> None:
    report = check_cost_budget(
        {"slow": {"duration_ms": 999_999.0, "tokens": 0.0}},
        per_scenario_budget={"max_duration_ms": 1000.0, "max_tokens": 200_000.0},
    )
    warnings = format_budget_warnings(report)
    assert len(warnings) == 1
    assert "slow" in warnings[0] and "duration_ms" in warnings[0]
