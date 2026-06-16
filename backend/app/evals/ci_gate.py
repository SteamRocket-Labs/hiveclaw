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
EXIT_ARTIFACT_GATE_FAILED = 5


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
    artifact_report: dict[str, Any] | None = None,
    adversarial_report: dict[str, Any] | None = None,
    require_live: bool = True,
    require_artifact_gate: bool = False,
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

    artifact_decision = _evaluate_ci_artifact_gate(
        artifact_report=artifact_report,
        adversarial_report=adversarial_report,
        require_artifact_gate=require_artifact_gate,
    )
    if artifact_decision is not None:
        return artifact_decision

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


def _artifact_report_passed(report: dict[str, Any] | None, *, required: bool) -> tuple[bool, str | None]:
    if not isinstance(report, dict):
        if required:
            return False, "artifact execution gate report missing"
        return True, None
    status = str(report.get("status") or "").strip().lower()
    if status == "not_applicable" and str(report.get("reason") or "").strip():
        return True, None
    if status == "passed" or bool(report.get("passed")):
        return True, None
    return False, str(report.get("reason") or report.get("error") or "artifact execution gate failed")


def _evaluate_ci_artifact_gate(
    *,
    artifact_report: dict[str, Any] | None,
    adversarial_report: dict[str, Any] | None,
    require_artifact_gate: bool,
) -> GateDecision | None:
    artifact_ok, artifact_reason = _artifact_report_passed(artifact_report, required=require_artifact_gate)
    if not artifact_ok:
        return GateDecision(
            passed=False,
            exit_code=EXIT_ARTIFACT_GATE_FAILED,
            reasons=[f"artifact execution gate failed (E6): {artifact_reason}"],
        )
    if adversarial_report is not None and not bool(adversarial_report.get("all_blocked")):
        return GateDecision(
            passed=False,
            exit_code=EXIT_ARTIFACT_GATE_FAILED,
            reasons=[f"adversarial suite failed (E9): {adversarial_report.get('attacks')}"],
        )
    return None


def _load_optional_json_report(path: Path | None, *, missing_payload: dict[str, Any]) -> dict[str, Any] | None:
    if path is None:
        return None
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return missing_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CI behavior-eval gate (E8).")
    parser.add_argument(
        "--behavior-report", type=Path, required=True, help="JSON behavior report from the Hive live runner"
    )
    parser.add_argument("--baseline", type=Path, required=True, help="path to a behavior_eval_baseline.v1 JSON file")
    parser.add_argument("--running-model", required=True)
    parser.add_argument(
        "--integrity-report",
        type=Path,
        help="JSON integrity report from app.evals.evaluator_integrity",
    )
    parser.add_argument(
        "--artifact-report",
        type=Path,
        help="JSON report from app.evals.artifact_gate for the candidate artifact",
    )
    parser.add_argument(
        "--adversarial-report",
        type=Path,
        help="JSON report from app.evals.adversarial_suite",
    )
    parser.add_argument(
        "--require-artifact-gate",
        action="store_true",
        help="fail closed when --artifact-report is missing or not passed",
    )
    parser.add_argument("--tolerance", type=float, default=0.0)
    parser.add_argument(
        "--allow-fallback", action="store_true", help="nightly observation only — never on the per-PR gate"
    )
    args = parser.parse_args(argv)

    report = json.loads(args.behavior_report.read_text(encoding="utf-8")) if args.behavior_report.is_file() else {}
    integrity = None
    if args.integrity_report is not None:
        if args.integrity_report.is_file():
            integrity = json.loads(args.integrity_report.read_text(encoding="utf-8"))
        else:
            integrity = {
                "trusted": False,
                "reason": f"integrity report unavailable: {args.integrity_report}",
            }
    artifact_report = _load_optional_json_report(
        args.artifact_report,
        missing_payload={
            "status": "missing_required",
            "passed": False,
            "reason": f"artifact report unavailable: {args.artifact_report}",
        },
    )
    adversarial_report = _load_optional_json_report(
        args.adversarial_report,
        missing_payload={
            "all_blocked": False,
            "reason": f"adversarial report unavailable: {args.adversarial_report}",
        },
    )

    from app.evals.baseline import BaselineUnavailableError, load_baseline

    try:
        baseline = load_baseline(args.baseline.stem, baselines_root=args.baseline.parent)
    except BaselineUnavailableError:
        baseline = None

    decision = evaluate_ci_gate(
        behavior_report=report,
        baseline=baseline,
        running_model=args.running_model,
        integrity=integrity,
        artifact_report=artifact_report,
        adversarial_report=adversarial_report,
        require_live=not args.allow_fallback,
        require_artifact_gate=args.require_artifact_gate,
        tolerance=args.tolerance,
    )
    print(f"[ci-gate] passed={decision.passed} exit={decision.exit_code} reasons={decision.reasons}")
    return decision.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
