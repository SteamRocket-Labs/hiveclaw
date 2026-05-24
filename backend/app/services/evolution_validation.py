"""Validation reports for the self-evolution ledger."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.evolution_ledger import load_evolution_ledger
from app.services.evolution_manifest import validate_evolution_manifest


PROMOTE_DECISIONS = {"promote", "promoted"}
HOLD_DECISIONS = {"hold", "held", "defer", "deferred"}
REJECT_DECISIONS = {"reject", "rejected"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check(
    check_id: str,
    status: str,
    message: str,
    *,
    candidate_id: str | None = None,
    evidence: dict[str, Any] | None = None,
    recommendation: str = "",
) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "candidate_id": candidate_id,
        "message": message,
        "evidence": evidence or {},
        "recommendation": recommendation,
    }


def _summarize_checks(checks: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"pass": 0, "warning": 0, "fail": 0}
    for check in checks:
        status = check.get("status")
        if status in summary:
            summary[status] += 1
    return summary


def _latest_eval(evals: list[dict[str, Any]]) -> dict[str, Any] | None:
    return evals[-1] if evals else None


def _candidate_maps(entries: list[dict[str, Any]]) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
]:
    candidates: dict[str, dict[str, Any]] = {}
    evals_by_candidate: dict[str, list[dict[str, Any]]] = {}
    decisions_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        candidate_id = str(entry.get("candidate_id") or "").strip()
        if not candidate_id:
            continue
        event = entry.get("event")
        if event == "candidate":
            candidates[candidate_id] = entry
        elif event == "eval_run":
            evals_by_candidate.setdefault(candidate_id, []).append(entry)
        elif event == "promotion_decision":
            decisions_by_candidate.setdefault(candidate_id, []).append(entry)
    return candidates, evals_by_candidate, decisions_by_candidate


def _validate_candidate(
    *,
    candidate_id: str,
    candidate: dict[str, Any],
    evals: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    source_attempts = [item for item in candidate.get("source_attempt_ids", []) if str(item).strip()]
    has_terminal_decision = bool(decisions)
    manifest_errors = validate_evolution_manifest(candidate.get("manifest"))
    checks.append(
        _check(
            "candidate_has_valid_manifest",
            "pass" if not manifest_errors else "fail",
            "Candidate has a valid self-evolution manifest."
            if not manifest_errors
            else "Candidate is missing a valid self-evolution manifest.",
            candidate_id=candidate_id,
            evidence={"errors": manifest_errors},
            recommendation=(
                "Record hive_evolution_manifest.v1 with source_refs, trace_refs, eval_refs, and rollback_plan before candidate persistence."
            ),
        )
    )
    checks.append(
        _check(
            "candidate_has_source_attempts",
            "pass" if source_attempts else "fail",
            "Candidate has source attempt traces." if source_attempts else "Candidate is missing source attempts.",
            candidate_id=candidate_id,
            evidence={"source_attempt_count": len(source_attempts)},
            recommendation="Create evolution candidates from RuntimeTask/session evidence, not free-floating edits.",
        )
    )

    if evals:
        checks.append(
            _check(
                "candidate_has_eval_run",
                "pass",
                "Candidate has at least one eval run.",
                candidate_id=candidate_id,
                evidence={"eval_run_count": len(evals)},
                recommendation="",
            )
        )
    else:
        checks.append(
            _check(
                "candidate_has_eval_run",
                "fail" if has_terminal_decision else "warning",
                "Candidate has no eval run.",
                candidate_id=candidate_id,
                evidence={"eval_run_count": 0, "has_terminal_decision": has_terminal_decision},
                recommendation="Run eval before holding or promoting a self-evolution candidate.",
            )
        )

    latest_eval = _latest_eval(evals)
    if latest_eval:
        traces = [item for item in latest_eval.get("traces", []) if str(item).strip()]
        has_reward = latest_eval.get("reward") is not None and latest_eval.get("baseline_reward") is not None
        checks.extend(
            [
                _check(
                    "eval_has_reward",
                    "pass" if has_reward else "fail",
                    "Eval run records reward and baseline."
                    if has_reward
                    else "Eval run is missing reward or baseline reward.",
                    candidate_id=candidate_id,
                    evidence={
                        "reward": latest_eval.get("reward"),
                        "baseline_reward": latest_eval.get("baseline_reward"),
                    },
                    recommendation="Record both reward and baseline_reward for bakeoff decisions.",
                ),
                _check(
                    "eval_has_traces",
                    "pass" if traces else "fail",
                    "Eval run has trace references." if traces else "Eval run has no trace references.",
                    candidate_id=candidate_id,
                    evidence={"trace_count": len(traces)},
                    recommendation="Attach RuntimeTask/session trace ids to every eval run.",
                ),
            ]
        )

    if not decisions:
        checks.append(
            _check(
                "candidate_has_promotion_decision",
                "warning",
                "Candidate has no promotion decision yet.",
                candidate_id=candidate_id,
                evidence={},
                recommendation="Record a hold/promote decision once eval is available.",
            )
        )
        return checks

    for decision in decisions:
        decision_value = str(decision.get("decision") or "").strip().lower()
        is_promoted = decision_value in PROMOTE_DECISIONS
        is_held = decision_value in HOLD_DECISIONS
        is_rejected = decision_value in REJECT_DECISIONS
        checks.append(
            _check(
                "candidate_has_promotion_decision",
                "pass",
                "Candidate has a promotion decision.",
                candidate_id=candidate_id,
                evidence={"decision": decision_value},
                recommendation="",
            )
        )
        if is_promoted:
            checks.extend(_validate_promotion_decision(candidate_id, decision, latest_eval))
        elif is_held:
            checks.append(
                _check(
                    "held_decision_has_reason",
                    "pass" if str(decision.get("reason") or "").strip() else "fail",
                    "Held decision records a reason."
                    if str(decision.get("reason") or "").strip()
                    else "Held decision is missing a reason.",
                    candidate_id=candidate_id,
                    evidence={"decision": decision_value},
                    recommendation="Explain why the candidate was held.",
                )
            )
        elif is_rejected:
            checks.append(
                _check(
                    "rejected_decision_has_reason",
                    "pass" if str(decision.get("reason") or "").strip() else "fail",
                    "Rejected decision records a reason."
                    if str(decision.get("reason") or "").strip()
                    else "Rejected decision is missing a reason.",
                    candidate_id=candidate_id,
                    evidence={"decision": decision_value},
                    recommendation="Explain which verification gate failed before rejecting the candidate.",
                )
            )
        else:
            checks.append(
                _check(
                    "promotion_decision_value_known",
                    "fail",
                    "Promotion decision has an unknown value.",
                    candidate_id=candidate_id,
                    evidence={"decision": decision_value},
                    recommendation="Use promote/promoted or hold/held decision values.",
                )
            )
    return checks


def _validate_promotion_decision(
    candidate_id: str,
    decision: dict[str, Any],
    latest_eval: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if latest_eval is None:
        return [
            _check(
                "promotion_has_rollback_ref",
                "fail" if not str(decision.get("rollback_ref") or "").strip() else "pass",
                "Promoted decision has rollback_ref."
                if str(decision.get("rollback_ref") or "").strip()
                else "Promoted decision is missing rollback_ref.",
                candidate_id=candidate_id,
                evidence={"rollback_ref": decision.get("rollback_ref")},
                recommendation="Record the exact file/version that can restore previous behavior.",
            ),
            _check(
                "promotion_blocks_critical_regression",
                "fail",
                "Promoted decision has no eval run to prove critical regressions are absent.",
                candidate_id=candidate_id,
                evidence={"has_eval_run": False},
                recommendation="Run eval and block promotion when critical_regressions > 0.",
            ),
        ]

    rollback_ref = str(decision.get("rollback_ref") or "").strip()
    critical_regressions = int(latest_eval.get("critical_regressions") or 0)
    passed = bool(latest_eval.get("passed"))
    reward = float(latest_eval.get("reward") or 0.0)
    baseline_reward = float(latest_eval.get("baseline_reward") or 0.0)
    return [
        _check(
            "promotion_has_rollback_ref",
            "pass" if rollback_ref else "fail",
            "Promoted decision has rollback_ref." if rollback_ref else "Promoted decision is missing rollback_ref.",
            candidate_id=candidate_id,
            evidence={"rollback_ref": rollback_ref or None},
            recommendation="Record the exact file/version that can restore previous behavior.",
        ),
        _check(
            "promotion_blocks_critical_regression",
            "pass" if critical_regressions == 0 else "fail",
            "Promoted decision has no critical regressions."
            if critical_regressions == 0
            else "Promoted decision ignored critical regressions.",
            candidate_id=candidate_id,
            evidence={"critical_regressions": critical_regressions},
            recommendation="Hold or roll back candidates with critical regressions.",
        ),
        _check(
            "promotion_requires_passing_eval",
            "pass" if passed else "fail",
            "Promoted decision is backed by a passing eval."
            if passed
            else "Promoted decision is not backed by a passing eval.",
            candidate_id=candidate_id,
            evidence={"passed": passed},
            recommendation="Do not promote candidates with failed eval runs.",
        ),
        _check(
            "promotion_reward_beats_baseline",
            "pass" if reward >= baseline_reward else "fail",
            "Promoted reward is at least baseline."
            if reward >= baseline_reward
            else "Promoted reward is below baseline.",
            candidate_id=candidate_id,
            evidence={"reward": reward, "baseline_reward": baseline_reward},
            recommendation="Hold or roll back candidates that do not beat baseline.",
        ),
    ]


def validate_evolution_ledger(workspace: Path, *, write_report: bool = True) -> dict[str, Any]:
    entries = load_evolution_ledger(workspace)
    candidates, evals_by_candidate, decisions_by_candidate = _candidate_maps(entries)
    checks: list[dict[str, Any]] = []

    if not entries:
        checks.append(
            _check(
                "ledger_has_entries",
                "warning",
                "Evolution ledger is empty.",
                evidence={},
                recommendation="Run self-evolution candidates through candidate/eval/decision ledger events.",
            )
        )
    else:
        checks.append(
            _check(
                "ledger_has_entries",
                "pass",
                "Evolution ledger has entries.",
                evidence={"entry_count": len(entries)},
                recommendation="",
            )
        )

    for candidate_id, candidate in candidates.items():
        checks.extend(
            _validate_candidate(
                candidate_id=candidate_id,
                candidate=candidate,
                evals=evals_by_candidate.get(candidate_id, []),
                decisions=decisions_by_candidate.get(candidate_id, []),
            )
        )

    for candidate_id, decisions in decisions_by_candidate.items():
        if candidate_id in candidates:
            continue
        for decision in decisions:
            checks.append(
                _check(
                    "decision_has_candidate",
                    "fail",
                    "Promotion decision references a missing candidate.",
                    candidate_id=candidate_id,
                    evidence={"decision": decision.get("decision")},
                    recommendation="Record evolution_candidate.v1 before eval or promotion decision.",
                )
            )

    summary = _summarize_checks(checks)
    generated_at = _now_iso()
    report: dict[str, Any] = {
        "schema": "evolution_validation_report.v1",
        "generated_at": generated_at,
        "passed": summary["fail"] == 0,
        "status": "fail" if summary["fail"] else ("warning" if summary["warning"] else "pass"),
        "summary": summary,
        "totals": {
            "entries": len(entries),
            "candidates": len(candidates),
            "eval_runs": sum(len(items) for items in evals_by_candidate.values()),
            "promotion_decisions": sum(len(items) for items in decisions_by_candidate.values()),
        },
        "checks": checks,
    }

    if write_report:
        path = workspace / "evolution" / "evolution_validation_report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        report["report_artifact"] = {
            "schema": "evolution_validation_report.v1",
            "path": "evolution/evolution_validation_report.json",
            "created_at": generated_at,
        }
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return report
