"""E10: per-scenario cost/latency budgets for behavior eval.

round2 §10 #8 (cache economy): a behavior eval that silently 10x's its token or
latency is a regression even when scores hold. The old cost_latency report only
checked that guards EXIST; this enforces a real per-scenario budget (max
duration_ms, max tokens) and flags any scenario that exceeds it. Per-PR treats a
violation as a soft warning; the nightly job records the time series.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_SCENARIO_BUDGET: dict[str, float] = {
    "max_duration_ms": 120_000.0,
    "max_tokens": 200_000.0,
}


@dataclass(frozen=True)
class BudgetViolation:
    scenario: str
    metric: str
    value: float
    budget: float

    def to_dict(self) -> dict[str, Any]:
        return {"scenario": self.scenario, "metric": self.metric, "value": self.value, "budget": self.budget}


def extract_scenario_costs(behavior_report: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Pull per-scenario (duration_ms, tokens) from a behavior report's score_breakdown."""

    costs: dict[str, dict[str, float]] = {}
    for name, entry in (behavior_report.get("scenarios") or {}).items():
        breakdown = entry.get("score_breakdown") or {} if isinstance(entry, dict) else {}
        costs[name] = {
            "duration_ms": float(breakdown.get("duration_ms") or 0.0),
            "tokens": float(breakdown.get("tokens") or breakdown.get("tokens_used") or 0.0),
        }
    return costs


def check_cost_budget(
    scenario_costs: dict[str, dict[str, float]],
    *,
    per_scenario_budget: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Flag scenarios exceeding the duration/token budget.

    A zero/absent token count is treated as "not measured" and never violates the
    token budget (avoids false alarms when token telemetry is unavailable).
    """

    budget = per_scenario_budget or DEFAULT_SCENARIO_BUDGET
    max_duration = float(budget.get("max_duration_ms", DEFAULT_SCENARIO_BUDGET["max_duration_ms"]))
    max_tokens = float(budget.get("max_tokens", DEFAULT_SCENARIO_BUDGET["max_tokens"]))
    violations: list[BudgetViolation] = []
    for name, cost in scenario_costs.items():
        duration = float(cost.get("duration_ms") or 0.0)
        if duration > max_duration:
            violations.append(BudgetViolation(name, "duration_ms", duration, max_duration))
        tokens = float(cost.get("tokens") or 0.0)
        if tokens > max_tokens:
            violations.append(BudgetViolation(name, "tokens", tokens, max_tokens))
    return {
        "within_budget": not violations,
        "violations": [violation.to_dict() for violation in violations],
    }


def format_budget_warnings(budget_report: dict[str, Any]) -> list[str]:
    """Human/CI-readable warnings (one per violation), e.g. for ::warning:: lines."""

    return [
        f"cost budget exceeded: scenario={v['scenario']} {v['metric']}={v['value']} > {v['budget']}"
        for v in budget_report.get("violations", [])
    ]
