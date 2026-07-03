"""Dynamic Workflow proposal, evidence, and repair helpers.

This module owns the Dynamic Workflow layer above the deterministic
``WorkflowDefinition`` runtime. It never executes workflow steps and never
accepts raw code/script surfaces; it only validates proposals, builds run
metadata, and summarizes journal evidence for repair/promotion decisions.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.runtime.workflow_admission import AdmissionLimits, WorkflowAdmissionError, admit_workflow, normalize_workflow_args
from app.runtime.workflow_compiler import WorkflowCompileError, compile_workflow
from app.runtime.workflow_definition import compute_definition_hash

DYNAMIC_PROPOSAL_NEXT_ACTION = (
    "Call preview_workflow with the selected candidate's lowered_definition and preview_args, "
    "show that exact preview to the user, then call start_workflow only after explicit approval."
)

FORBIDDEN_DYNAMIC_CANDIDATE_KEYS = {
    "code",
    "script",
    "javascript",
    "typescript",
    "python",
    "shell",
    "eval",
    "jinja",
}


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _reject_dynamic_code_surface(candidate: dict[str, Any]) -> str | None:
    forbidden = sorted(FORBIDDEN_DYNAMIC_CANDIDATE_KEYS & {str(key).lower() for key in candidate})
    if forbidden:
        return f"dynamic workflow candidates cannot contain executable code fields: {', '.join(forbidden)}"
    return None


def _proposal_candidate_id(candidate: dict[str, Any], index: int) -> str:
    raw = str(candidate.get("candidate_id") or candidate.get("id") or "").strip()
    return raw or f"candidate-{index + 1}"


def admit_dynamic_candidate(
    *,
    candidate: dict[str, Any],
    index: int,
    proposal_args: dict[str, Any],
) -> dict[str, Any]:
    code_error = _reject_dynamic_code_surface(candidate)
    if code_error:
        raise WorkflowCompileError(code_error)
    lowered_definition = candidate.get("lowered_definition")
    if not isinstance(lowered_definition, dict):
        raise WorkflowCompileError(f"candidate {_proposal_candidate_id(candidate, index)!r} lowered_definition must be an object")

    compiled = compile_workflow(lowered_definition)
    preview_args = normalize_workflow_args(compiled, mapping(candidate.get("args")) or proposal_args)
    from app.config import get_settings
    from app.services.workflow_launch import inspect_workflow_confirmation_needs

    admission = admit_workflow(compiled, args=preview_args, limits=AdmissionLimits.from_settings(get_settings()))
    confirmation = inspect_workflow_confirmation_needs(compiled, args=preview_args)
    return {
        "candidate_id": _proposal_candidate_id(candidate, index),
        "name": str(candidate.get("name") or compiled.definition.name),
        "pattern_mix": string_list(candidate.get("pattern_mix")),
        "risk_level": str(candidate.get("risk_level") or "medium"),
        "budget": mapping(candidate.get("budget")),
        "failure_policy": mapping(candidate.get("failure_policy")),
        "success_criteria": string_list(candidate.get("success_criteria")),
        "lowered_definition": compiled.definition.canonical_dict(),
        "preview_args": preview_args,
        "definition_hash": compiled.definition_hash,
        "args_hash": compute_definition_hash(preview_args),
        "confirmation_required": confirmation.requires_confirmation,
        "confirmation_reasons": confirmation.reasons,
        "planned_leaf_calls": admission.planned_leaf_calls,
        "budget_tokens": admission.budget_tokens,
    }


def validate_dynamic_workflow_proposal(arguments: dict[str, Any]) -> dict[str, Any]:
    candidates_input = arguments.get("candidates")
    if not isinstance(candidates_input, list) or not candidates_input:
        return {"ok": False, "error": "propose_dynamic_workflow requires at least one candidate"}

    proposal_args = mapping(arguments.get("args"))
    accepted: list[dict[str, Any]] = []
    try:
        for index, raw_candidate in enumerate(candidates_input):
            if not isinstance(raw_candidate, dict):
                raise WorkflowCompileError(f"candidate {index + 1} must be an object")
            accepted.append(admit_dynamic_candidate(candidate=raw_candidate, index=index, proposal_args=proposal_args))
    except (WorkflowCompileError, WorkflowAdmissionError) as exc:
        return {"ok": False, "error": f"invalid lowered_definition: {exc}"}

    proposal_success_criteria = string_list(arguments.get("success_criteria"))
    accepted = [
        {
            **candidate,
            "success_criteria": candidate["success_criteria"] or proposal_success_criteria,
        }
        for candidate in accepted
    ]
    requested_recommended = str(arguments.get("recommended_candidate_id") or "").strip()
    candidate_ids = {candidate["candidate_id"] for candidate in accepted}
    recommended_candidate_id = requested_recommended if requested_recommended in candidate_ids else accepted[0]["candidate_id"]
    proposal_id = str(arguments.get("proposal_id") or "").strip() or f"dwf-{uuid.uuid4()}"
    return {
        "ok": True,
        "status": "dynamic_workflow_proposed",
        "proposal_id": proposal_id,
        "goal": str(arguments.get("goal") or "").strip(),
        "why_workflow": str(arguments.get("why_workflow") or "").strip(),
        "success_criteria": proposal_success_criteria,
        "recommended_candidate_id": recommended_candidate_id,
        "candidates": accepted,
        "next_action": DYNAMIC_PROPOSAL_NEXT_ACTION,
    }


def build_dynamic_workflow_run_metadata(
    *,
    proposal_id: str | None,
    candidate_id: str | None,
    preview_id: str | None,
    definition_hash: str,
    args_hash: str,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not proposal_id and not candidate_id:
        return None
    candidate = candidate or {}
    return {
        "dynamic_workflow": {
            "proposal_id": proposal_id,
            "candidate_id": candidate_id,
            "preview_id": preview_id,
            "definition_hash": definition_hash,
            "args_hash": args_hash,
            "pattern_mix": string_list(candidate.get("pattern_mix")),
            "success_criteria": string_list(candidate.get("success_criteria")),
            "failure_policy": mapping(candidate.get("failure_policy")),
            "budget": mapping(candidate.get("budget")),
        }
    }


def _status_count(rows: list[Any], status: str) -> int:
    return sum(1 for row in rows if getattr(row, "status", None) == status)


def summarize_dynamic_workflow_outcome(*, task: Any, steps: list[Any], leaf_calls: list[Any]) -> dict[str, Any]:
    status = getattr(task, "status", None)
    steps_total = len(steps)
    leaf_total = len(leaf_calls)
    evidence = {
        "status": status,
        "steps_total": steps_total,
        "steps_done": _status_count(steps, "done"),
        "steps_failed": _status_count(steps, "failed"),
        "steps_suspended": _status_count(steps, "suspended"),
        "leaf_total": leaf_total,
        "leaf_done": _status_count(leaf_calls, "done"),
        "leaf_failed": _status_count(leaf_calls, "failed"),
        "promotion_eligible": bool(status == "completed" and _status_count(steps, "failed") == 0 and _status_count(leaf_calls, "failed") == 0),
    }
    metadata = getattr(task, "metadata_json", None) or {}
    dynamic = mapping(metadata.get("dynamic_workflow"))
    success_criteria = string_list(dynamic.get("success_criteria"))
    if success_criteria:
        evidence["success_criteria_count"] = len(success_criteria)
    return evidence


def build_dynamic_workflow_repair_plan(*, task: Any, steps: list[Any], leaf_calls: list[Any]) -> dict[str, Any] | None:
    metadata = getattr(task, "metadata_json", None) or {}
    dynamic = mapping(metadata.get("dynamic_workflow"))
    if not dynamic:
        return None

    failed_leaves = [
        {
            "step_id": getattr(leaf, "step_id", None),
            "leaf_id": getattr(leaf, "leaf_id", None),
            "error": getattr(leaf, "error", None),
        }
        for leaf in leaf_calls
        if getattr(leaf, "status", None) == "failed"
    ]
    failed_steps = [
        {
            "step_id": getattr(step, "step_id", None),
            "error": getattr(step, "error", None),
        }
        for step in steps
        if getattr(step, "status", None) == "failed"
    ]
    repair_rounds = int(mapping(dynamic.get("failure_policy")).get("repair_rounds") or 1)
    repair_attempts = int(dynamic.get("repair_attempts") or 0)
    repairable = bool(getattr(task, "status", None) in {"failed", "suspended", "killed"} and repair_attempts < repair_rounds and (failed_leaves or failed_steps))
    return {
        "repairable": repairable,
        "strategy": "resume_failed_leaves" if failed_leaves else "resume_from_failed_step",
        "repair_attempts": repair_attempts,
        "repair_rounds": repair_rounds,
        "failed_leaf_count": len(failed_leaves),
        "failed_step_count": len(failed_steps),
        "failed_leaves": failed_leaves[:20],
        "failed_steps": failed_steps[:20],
        "next_action": "repair_workflow_run" if repairable else None,
    }


def dump_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)
