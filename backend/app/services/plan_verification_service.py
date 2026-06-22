"""Deterministic plan verification contract.

This is the evidence gate around a plan artifact. It does not decide whether
the plan is good; it verifies whether claimed execution has enough explicit
evidence to satisfy the plan's own success criteria.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item.get("description") if isinstance(item, dict) else item or "").strip()
        if text:
            result.append(text)
    return result


def verify_plan_artifact(
    *,
    plan_json: dict[str, Any],
    evidence_refs: list[str] | None = None,
    completed_criteria: list[str] | None = None,
    failed_criteria: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    criteria = _string_list((plan_json or {}).get("success_criteria"))
    evidence = [str(ref).strip() for ref in (evidence_refs or []) if str(ref).strip()]
    completed = [str(item).strip() for item in (completed_criteria or []) if str(item).strip()]
    failed = [str(item).strip() for item in (failed_criteria or []) if str(item).strip()]

    if not criteria:
        return {
            "status": "blocked",
            "passed": False,
            "reason": "plan_missing_success_criteria",
            "success_criteria": [],
            "missing_criteria": [],
            "failed_criteria": failed,
            "evidence_refs": evidence,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "notes": notes,
        }

    if failed:
        status = "failed"
        missing = [criterion for criterion in criteria if criterion not in completed]
        reason = "failed_criteria_present"
    elif completed:
        missing = [criterion for criterion in criteria if criterion not in completed]
        status = "passed" if not missing and evidence else "blocked"
        reason = "ok" if status == "passed" else "missing_completed_criteria_or_evidence"
    else:
        missing = [] if evidence else list(criteria)
        status = "passed" if evidence else "blocked"
        reason = "ok" if status == "passed" else "missing_evidence_refs"

    return {
        "status": status,
        "passed": status == "passed",
        "reason": reason,
        "success_criteria": criteria,
        "missing_criteria": missing,
        "failed_criteria": failed,
        "evidence_refs": evidence,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "notes": notes,
    }
