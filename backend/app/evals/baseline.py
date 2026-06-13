"""E1: hardened, version-pinned baselines for external behavior eval.

A baseline snapshot is the single source of truth for "did behavior regress".
It is a git-tracked artifact under ``app/evals/baselines/`` carrying the model,
date, and commit that produced it, plus per-scenario p50 scores and variance.

Design invariants (round2 §2.3 / §9):
- **Fail-closed**: a missing or invalid baseline raises, never silently passes.
- **Model-pinned**: comparing against a baseline produced by a different model
  is refused — the caller must explicitly rebaseline (guards silent drift, G7).
- **Read-only at compare time**: this module never writes baselines; promotion
  of a new baseline is a separate, governed step (E5 wires the protected source).

This is functional core: pure data + validation, no DB/LLM/IO beyond reading the
snapshot file. The protected-source trust root (CODEOWNERS / protected base
branch) is layered on in E5; here we only guarantee fail-closed loading,
schema validity, regression comparison, and model-match.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BEHAVIOR_EVAL_BASELINE_SCHEMA = "behavior_eval_baseline.v1"

_REQUIRED_TOP_FIELDS = (
    "schema",
    "suite",
    "baseline_version",
    "baseline_model",
    "baseline_date",
    "commit_sha",
    "scenarios",
)


class BaselineUnavailableError(RuntimeError):
    """A required baseline snapshot is missing, unreadable, or invalid (fail-closed)."""


class BaselineModelMismatchError(RuntimeError):
    """Running model differs from the baseline model — explicit rebaseline required."""


@dataclass(frozen=True, slots=True)
class ScenarioRegression:
    scenario: str
    baseline_p50: float
    current_p50: float
    delta: float
    regressed: bool


@dataclass(frozen=True, slots=True)
class RegressionReport:
    suite: str
    passed: bool
    regressions: list[ScenarioRegression] = field(default_factory=list)
    missing_scenarios: list[str] = field(default_factory=list)

    @property
    def regressed_scenarios(self) -> list[str]:
        return [r.scenario for r in self.regressions if r.regressed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "passed": self.passed,
            "missing_scenarios": list(self.missing_scenarios),
            "regressions": [
                {
                    "scenario": r.scenario,
                    "baseline_p50": r.baseline_p50,
                    "current_p50": r.current_p50,
                    "delta": r.delta,
                    "regressed": r.regressed,
                }
                for r in self.regressions
            ],
        }


def validate_baseline(payload: dict[str, Any]) -> list[str]:
    """Return a list of validation errors; an empty list means the baseline is valid."""

    errors: list[str] = []
    for name in _REQUIRED_TOP_FIELDS:
        if name not in payload or payload[name] in (None, ""):
            errors.append(f"missing required field: {name}")
    schema = payload.get("schema")
    if schema is not None and schema != BEHAVIOR_EVAL_BASELINE_SCHEMA:
        errors.append(f"unexpected schema: {schema!r}")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, dict) or not scenarios:
        errors.append("scenarios must be a non-empty object")
    else:
        for scenario_name, entry in scenarios.items():
            if not isinstance(entry, dict) or "score_p50" not in entry:
                errors.append(f"scenario {scenario_name} missing score_p50")
    return errors


def load_baseline(suite: str, *, baselines_root: Path) -> dict[str, Any]:
    """Load and validate a baseline snapshot; fail-closed on any problem."""

    path = baselines_root / f"{suite}.json"
    if not path.exists():
        raise BaselineUnavailableError(f"baseline snapshot not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineUnavailableError(f"baseline unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BaselineUnavailableError(f"baseline is not an object: {path}")
    errors = validate_baseline(payload)
    if errors:
        raise BaselineUnavailableError(f"baseline invalid: {path}: {'; '.join(errors)}")
    return payload


def check_model_match(baseline: dict[str, Any], *, running_model: str) -> None:
    """Refuse comparison when the running model differs from the baseline model (G7)."""

    baseline_model = baseline.get("baseline_model")
    if baseline_model != running_model:
        raise BaselineModelMismatchError(
            f"running model {running_model!r} != baseline model {baseline_model!r}; "
            "explicit rebaseline required before regression comparison"
        )


def compare_to_baseline(
    current_scores: dict[str, float],
    baseline: dict[str, Any],
    *,
    tolerance: float = 0.0,
) -> RegressionReport:
    """Per-scenario regression check.

    A scenario regresses when ``current < baseline_p50 - tolerance``. A scenario
    present in the baseline but absent from ``current_scores`` is a coverage gap
    and fails the report (we cannot prove non-regression for what we did not run).
    """

    scenarios = baseline.get("scenarios", {})
    regressions: list[ScenarioRegression] = []
    missing: list[str] = []
    for scenario_name, entry in scenarios.items():
        baseline_p50 = float(entry.get("score_p50", 0.0))
        if scenario_name not in current_scores:
            missing.append(scenario_name)
            continue
        current = float(current_scores[scenario_name])
        delta = current - baseline_p50
        regressed = current < (baseline_p50 - tolerance)
        regressions.append(
            ScenarioRegression(
                scenario=scenario_name,
                baseline_p50=baseline_p50,
                current_p50=current,
                delta=delta,
                regressed=regressed,
            )
        )
    passed = not missing and not any(r.regressed for r in regressions)
    return RegressionReport(
        suite=str(baseline.get("suite", "")),
        passed=passed,
        regressions=regressions,
        missing_scenarios=missing,
    )
