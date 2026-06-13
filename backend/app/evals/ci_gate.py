"""E8: the CI behavior-eval gate — decides exit code from E1/E2/E5 results.

Two layers (decision D2): per-PR runs the deterministic behavior subset + a Hive
live smoke and BLOCKS MERGE on regression or a required-live fallback; nightly
runs the full suite + variance + Hermes cross + rubric observation.

The decision here is pure: given a behavior report (E2), a baseline (E1), and an
evaluator-integrity verdict (E5), compute pass/fail + a non-zero exit code that
blocks the merge. The actual agent execution (invoke_agent, needs DB+LLM) runs in
the CI job that produces the behavior report and calls this gate.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_REQUIRED_LIVE_FALLBACK = 2
EXIT_UNTRUSTED_EVALUATOR = 3
EXIT_BASELINE_UNAVAILABLE = 4


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    exit_code: int
    reasons: list[str] = field(default_factory=list)


def evaluate_ci_gate(
    *,
    behavior_report: dict[str, Any],
    baseline: dict[str, Any] | None,
    running_model: str,
    integrity: dict[str, Any] | None = None,
    require_live: bool = True,
    tolerance: float = 0.0,
) -> GateDecision:
    """Compose the hard gates: evaluator trust (E5) -> live run (E2) -> baseline /
    no-regression (E1). The first failing gate decides a distinct non-zero exit."""

    # E5: the evaluator that produced this verdict must be trusted.
    if integrity is not None and not integrity.get("trusted", False):
        return GateDecision(
            passed=False,
            exit_code=EXIT_UNTRUSTED_EVALUATOR,
            reasons=[f"evaluator untrusted (E5): {integrity.get('reason', '')}".strip()],
        )

    # E2: a passing gate requires a complete, trusted live run — fallbacks never pass.
    from app.evals.hive_live_runner import behavior_eval_passed

    if require_live and not behavior_eval_passed(behavior_report):
        return GateDecision(
            passed=False,
            exit_code=EXIT_REQUIRED_LIVE_FALLBACK,
            reasons=[f"required live run not satisfied (transport={behavior_report.get('transport')})"],
        )

    # E1: baseline must exist (fail-closed) and the model must match.
    if not isinstance(baseline, dict):
        return GateDecision(
            passed=False,
            exit_code=EXIT_BASELINE_UNAVAILABLE,
            reasons=["baseline unavailable (fail-closed)"],
        )
    from app.evals.baseline import BaselineModelMismatchError, check_model_match, compare_to_baseline

    try:
        check_model_match(baseline, running_model=running_model)
    except BaselineModelMismatchError as exc:
        return GateDecision(passed=False, exit_code=EXIT_REGRESSION, reasons=[f"baseline model drift: {exc}"])

    current_scores = {
        name: float(entry.get("score") or 0.0) for name, entry in (behavior_report.get("scenarios") or {}).items()
    }
    regression = compare_to_baseline(current_scores, baseline, tolerance=tolerance)
    if not regression.passed:
        return GateDecision(
            passed=False,
            exit_code=EXIT_REGRESSION,
            reasons=[
                f"behavior regression vs baseline: regressed={regression.regressed_scenarios} "
                f"missing={regression.missing_scenarios}"
            ],
        )

    return GateDecision(
        passed=True,
        exit_code=EXIT_OK,
        reasons=["behavior eval passed, no regression, evaluator trusted"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CI behavior-eval gate (E8).")
    parser.add_argument(
        "--behavior-report", type=Path, required=True, help="JSON behavior report from the Hive live runner"
    )
    parser.add_argument("--baseline", type=Path, required=True, help="path to a behavior_eval_baseline.v1 JSON file")
    parser.add_argument("--running-model", required=True)
    parser.add_argument("--tolerance", type=float, default=0.0)
    parser.add_argument(
        "--allow-fallback", action="store_true", help="nightly observation only — never on the per-PR gate"
    )
    args = parser.parse_args(argv)

    report = json.loads(args.behavior_report.read_text(encoding="utf-8")) if args.behavior_report.is_file() else {}

    from app.evals.baseline import BaselineUnavailableError, load_baseline

    try:
        baseline = load_baseline(args.baseline.stem, baselines_root=args.baseline.parent)
    except BaselineUnavailableError:
        baseline = None

    decision = evaluate_ci_gate(
        behavior_report=report,
        baseline=baseline,
        running_model=args.running_model,
        require_live=not args.allow_fallback,
        tolerance=args.tolerance,
    )
    print(f"[ci-gate] passed={decision.passed} exit={decision.exit_code} reasons={decision.reasons}")
    return decision.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
