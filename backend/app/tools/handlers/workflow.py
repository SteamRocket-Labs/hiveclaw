"""Workflow tools: preview + start for ephemeral definitions.

The agent submits DEFINITION DATA only (§3.2) — the engine interprets it;
there is no code execution surface. ``preview_workflow`` surfaces deterministic
confirmation notes, but ``start_workflow`` never enters Plan Mode automatically.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from app.runtime.workflow_admission import AdmissionLimits, WorkflowAdmissionError, admit_workflow
from app.runtime.workflow_compiler import WorkflowCompileError, compile_workflow
from app.runtime.workflow_definition import compute_definition_hash
from app.services.workflow_launch import inspect_workflow_confirmation_needs, start_ephemeral_workflow_for_agent
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest

_PREVIEW_TTL_SECONDS = 60 * 60
_WORKFLOW_PREVIEW_CACHE: dict[str, dict[str, Any]] = {}


def _prune_preview_cache(now: float | None = None) -> None:
    now = now or time.monotonic()
    expired = [
        preview_id
        for preview_id, record in _WORKFLOW_PREVIEW_CACHE.items()
        if now - float(record.get("created_at", 0.0)) > _PREVIEW_TTL_SECONDS
    ]
    for preview_id in expired:
        _WORKFLOW_PREVIEW_CACHE.pop(preview_id, None)


def _record_preview(
    *,
    agent_id: uuid.UUID,
    definition_hash: str,
    args_hash: str,
    confirmation_required: bool,
    proposal_id: str | None = None,
    candidate_id: str | None = None,
) -> str:
    _prune_preview_cache()
    preview_id = str(uuid.uuid4())
    _WORKFLOW_PREVIEW_CACHE[preview_id] = {
        "agent_id": str(agent_id),
        "definition_hash": definition_hash,
        "args_hash": args_hash,
        "confirmation_required": confirmation_required,
        "proposal_id": proposal_id,
        "candidate_id": candidate_id,
        "created_at": time.monotonic(),
    }
    return preview_id


def _validate_preview_binding(
    *,
    agent_id: uuid.UUID,
    definition: dict,
    args: dict,
    preview_id: str | None,
    expected_definition_hash: str | None,
    expected_args_hash: str | None,
) -> tuple[bool, str]:
    compiled = compile_workflow(definition)
    actual_definition_hash = compiled.definition_hash
    actual_args_hash = compute_definition_hash(args)

    if preview_id:
        _prune_preview_cache()
        record = _WORKFLOW_PREVIEW_CACHE.get(preview_id)
        if record is None:
            return False, "start_workflow requires a fresh preview_workflow result; preview_id is unknown or expired"
        if record.get("agent_id") != str(agent_id):
            return False, "start_workflow preview_id belongs to another agent"
        if record.get("definition_hash") != actual_definition_hash or record.get("args_hash") != actual_args_hash:
            return False, "start_workflow definition/args differ from the preview_workflow artifact"
        return True, ""

    if expected_definition_hash or expected_args_hash:
        if not expected_definition_hash or not expected_args_hash:
            return False, "start_workflow requires both definition_hash and args_hash when preview_id is omitted"
        if expected_definition_hash != actual_definition_hash:
            return False, "start_workflow definition_hash does not match the supplied definition"
        if expected_args_hash != actual_args_hash:
            return False, "start_workflow args_hash does not match the supplied args"
        return True, ""

    return False, "start_workflow requires preview_workflow first; pass preview_id from preview_workflow"


_DEFINITION_PARAM = {
    "type": "object",
    "description": (
        "Structured workflow definition (data, not code): name, args_schema, steps "
        "(agent_step/fanout_step/gate_step/wait_until_step). Task strings may reference "
        "{{args.x}} and {{steps.<id>.output}} (pure key substitution)."
    ),
}

_DYNAMIC_PROPOSAL_NEXT_ACTION = (
    "Call preview_workflow with the selected candidate's lowered_definition and preview_args, "
    "show that exact preview to the user, then call start_workflow only after explicit approval."
)
_FORBIDDEN_DYNAMIC_CANDIDATE_KEYS = {
    "code",
    "script",
    "javascript",
    "typescript",
    "python",
    "shell",
    "eval",
    "jinja",
}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _reject_dynamic_code_surface(candidate: dict[str, Any]) -> str | None:
    forbidden = sorted(_FORBIDDEN_DYNAMIC_CANDIDATE_KEYS & {str(key).lower() for key in candidate})
    if forbidden:
        return f"dynamic workflow candidates cannot contain executable code fields: {', '.join(forbidden)}"
    return None


def _proposal_candidate_id(candidate: dict[str, Any], index: int) -> str:
    raw = str(candidate.get("candidate_id") or candidate.get("id") or "").strip()
    return raw or f"candidate-{index + 1}"


def _admit_dynamic_candidate(
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
    preview_args = _mapping(candidate.get("args")) or proposal_args
    compiled = compile_workflow(lowered_definition)
    from app.config import get_settings

    admission = admit_workflow(compiled, args=preview_args, limits=AdmissionLimits.from_settings(get_settings()))
    confirmation = inspect_workflow_confirmation_needs(compiled, args=preview_args)
    return {
        "candidate_id": _proposal_candidate_id(candidate, index),
        "name": str(candidate.get("name") or compiled.definition.name),
        "pattern_mix": _string_list(candidate.get("pattern_mix")),
        "risk_level": str(candidate.get("risk_level") or "medium"),
        "budget": _mapping(candidate.get("budget")),
        "failure_policy": _mapping(candidate.get("failure_policy")),
        "lowered_definition": compiled.definition.canonical_dict(),
        "preview_args": preview_args,
        "definition_hash": compiled.definition_hash,
        "args_hash": compute_definition_hash(preview_args),
        "confirmation_required": confirmation.requires_confirmation,
        "confirmation_reasons": confirmation.reasons,
        "planned_leaf_calls": admission.planned_leaf_calls,
        "budget_tokens": admission.budget_tokens,
    }


@tool(
    ToolMeta(
        name="propose_dynamic_workflow",
        description=(
            "Draft and validate Dynamic Workflow candidates WITHOUT previewing or starting execution.\n\n"
            "Use when the task needs many isolated workers, repeatable orchestration, adversarial review, "
            "or long-running state outside the main context. Provide one to three candidates with success criteria, "
            "pattern mix, budget, failure policy, and a lowered governed WorkflowDefinition. "
            "This tool never runs workflow steps. After choosing a candidate, call preview_workflow with the returned "
            "lowered_definition and preview_args; start_workflow only after the user approves that exact preview."
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "why_workflow": {"type": "string"},
                "success_criteria": {"type": "array", "items": {"type": "string"}},
                "args": {"type": "object", "description": "Preview args shared by candidates unless overridden."},
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "candidate_id": {"type": "string"},
                            "name": {"type": "string"},
                            "pattern_mix": {"type": "array", "items": {"type": "string"}},
                            "risk_level": {"type": "string"},
                            "budget": {"type": "object"},
                            "failure_policy": {"type": "object"},
                            "args": {"type": "object"},
                            "lowered_definition": _DEFINITION_PARAM,
                        },
                        "required": ["lowered_definition"],
                    },
                },
                "recommended_candidate_id": {"type": "string"},
            },
            "required": ["goal", "why_workflow", "success_criteria", "candidates"],
        },
        category="workflow",
        display_name="Propose Dynamic Workflow",
        read_only=True,
        parallel_safe=True,
        governance="safe",
    )
)
async def propose_dynamic_workflow(agent_id: uuid.UUID, arguments: dict) -> str:
    _ = agent_id
    candidates_input = arguments.get("candidates")
    if not isinstance(candidates_input, list) or not candidates_input:
        return json.dumps({"ok": False, "error": "propose_dynamic_workflow requires at least one candidate"}, ensure_ascii=False)
    proposal_args = _mapping(arguments.get("args"))
    accepted: list[dict[str, Any]] = []
    try:
        for index, raw_candidate in enumerate(candidates_input):
            if not isinstance(raw_candidate, dict):
                raise WorkflowCompileError(f"candidate {index + 1} must be an object")
            accepted.append(
                _admit_dynamic_candidate(candidate=raw_candidate, index=index, proposal_args=proposal_args)
            )
    except (WorkflowCompileError, WorkflowAdmissionError) as exc:
        return json.dumps({"ok": False, "error": f"invalid lowered_definition: {exc}"}, ensure_ascii=False)

    requested_recommended = str(arguments.get("recommended_candidate_id") or "").strip()
    candidate_ids = {candidate["candidate_id"] for candidate in accepted}
    recommended_candidate_id = requested_recommended if requested_recommended in candidate_ids else accepted[0]["candidate_id"]
    proposal_id = str(arguments.get("proposal_id") or "").strip() or f"dwf-{uuid.uuid4()}"
    return json.dumps(
        {
            "ok": True,
            "status": "dynamic_workflow_proposed",
            "proposal_id": proposal_id,
            "goal": str(arguments.get("goal") or "").strip(),
            "why_workflow": str(arguments.get("why_workflow") or "").strip(),
            "success_criteria": _string_list(arguments.get("success_criteria")),
            "recommended_candidate_id": recommended_candidate_id,
            "candidates": accepted,
            "next_action": _DYNAMIC_PROPOSAL_NEXT_ACTION,
        },
        ensure_ascii=False,
    )


@tool(
    ToolMeta(
        name="preview_workflow",
        description=(
            "Compile and preflight an ephemeral workflow definition WITHOUT running it.\n\n"
            "Usage:\n"
            "- Always preview before start_workflow: returns preview_id, definition_hash, args_hash, confirmation notes, "
            "planned leaf calls and budget.\n"
            "- Confirmation notes are informational; they do not force Plan Mode.\n"
            "- Show the user the preview and get their go-ahead before starting."
        ),
        parameters={
            "type": "object",
            "properties": {
                "definition": _DEFINITION_PARAM,
                "args": {"type": "object", "description": "Arguments matching the definition's args_schema."},
            },
            "required": ["definition"],
        },
        category="workflow",
        display_name="Preview Workflow",
        read_only=True,
        parallel_safe=True,
        governance="safe",
    )
)
async def preview_workflow(agent_id: uuid.UUID, arguments: dict) -> str:
    definition = arguments.get("definition") or {}
    args = arguments.get("args") or {}
    proposal_id = str(arguments.get("proposal_id") or "").strip() or None
    candidate_id = str(arguments.get("candidate_id") or "").strip() or None
    try:
        compiled = compile_workflow(definition)
        from app.config import get_settings

        admission = admit_workflow(compiled, args=args, limits=AdmissionLimits.from_settings(get_settings()))
        confirmation = inspect_workflow_confirmation_needs(compiled, args=args)
    except (WorkflowCompileError, WorkflowAdmissionError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    return json.dumps(
        {
            "ok": True,
            "preview_id": _record_preview(
                agent_id=agent_id,
                definition_hash=compiled.definition_hash,
                args_hash=compute_definition_hash(args),
                confirmation_required=confirmation.requires_confirmation,
                proposal_id=proposal_id,
                candidate_id=candidate_id,
            ),
            "proposal_id": proposal_id,
            "candidate_id": candidate_id,
            "definition_hash": compiled.definition_hash,
            "args_hash": compute_definition_hash(args),
            "confirmation_required": confirmation.requires_confirmation,
            "confirmation_reasons": confirmation.reasons,
            "planned_leaf_calls": admission.planned_leaf_calls,
            "budget_tokens": admission.budget_tokens,
        },
        ensure_ascii=False,
    )


@tool(
    ToolMeta(
        name="start_workflow",
        description=(
            "Start an ephemeral workflow run from a structured definition.\n\n"
            "Use a workflow ONLY when the step order itself is a requirement — a fixed sequence "
            "that must not drift, mandatory mid-run approval gates, or large fan-out under a hard "
            "budget. For one-off parallelism or isolation, spawn_subagent is enough; for handing "
            "work to another digital employee, use delegate_to_agent.\n\n"
            "Usage:\n"
            "- preview_workflow FIRST and show the user what will run.\n"
            "- If preview shows confirmation notes, obtain explicit user go-ahead before starting; "
            "do not enter Plan Mode unless the user explicitly asks for it.\n"
            "- Pass ledger_todo_id to mirror the run onto your work-ledger todo."
        ),
        parameters={
            "type": "object",
            "properties": {
                "definition": _DEFINITION_PARAM,
                "args": {"type": "object", "description": "Arguments matching the definition's args_schema."},
                "preview_id": {
                    "type": "string",
                    "description": "The preview_id returned by preview_workflow for this exact definition and args.",
                },
                "definition_hash": {
                    "type": "string",
                    "description": "Fallback binding hash returned by preview_workflow when preview_id is unavailable.",
                },
                "args_hash": {
                    "type": "string",
                    "description": "Fallback args hash returned by preview_workflow when preview_id is unavailable.",
                },
                "ledger_todo_id": {
                    "type": "string",
                    "description": "Optional work-ledger todo this run serves (observation mirror only).",
                },
            },
            "required": ["definition"],
        },
        category="workflow",
        display_name="Start Workflow",
        governance="sensitive",
    )
)
async def start_workflow(request: ToolExecutionRequest) -> str:
    agent_id = request.context.agent_id
    arguments = request.arguments
    definition = arguments.get("definition") or {}
    args = arguments.get("args") or {}
    ledger_todo_id = arguments.get("ledger_todo_id") or None
    preview_id = str(arguments.get("preview_id") or "").strip() or None
    definition_hash = str(arguments.get("definition_hash") or "").strip() or None
    args_hash = str(arguments.get("args_hash") or "").strip() or None
    proposal_id = str(arguments.get("proposal_id") or "").strip() or None
    candidate_id = str(arguments.get("candidate_id") or "").strip() or None
    try:
        preview_ok, preview_error = _validate_preview_binding(
            agent_id=agent_id,
            definition=definition,
            args=args,
            preview_id=preview_id,
            expected_definition_hash=definition_hash,
            expected_args_hash=args_hash,
        )
        if not preview_ok:
            return json.dumps({"ok": False, "error": preview_error}, ensure_ascii=False)
        preview_record = _WORKFLOW_PREVIEW_CACHE.get(preview_id) if preview_id else None
        preview_proposal_id = str((preview_record or {}).get("proposal_id") or "").strip() or None
        preview_candidate_id = str((preview_record or {}).get("candidate_id") or "").strip() or None
        if proposal_id and preview_proposal_id and proposal_id != preview_proposal_id:
            return json.dumps({"ok": False, "error": "start_workflow proposal_id differs from preview_workflow"}, ensure_ascii=False)
        if candidate_id and preview_candidate_id and candidate_id != preview_candidate_id:
            return json.dumps({"ok": False, "error": "start_workflow candidate_id differs from preview_workflow"}, ensure_ascii=False)
        proposal_id = proposal_id or preview_proposal_id
        candidate_id = candidate_id or preview_candidate_id
        run_metadata = None
        if proposal_id or candidate_id:
            run_metadata = {
                "dynamic_workflow": {
                    "proposal_id": proposal_id,
                    "candidate_id": candidate_id,
                    "preview_id": preview_id,
                    "definition_hash": compile_workflow(definition).definition_hash,
                    "args_hash": compute_definition_hash(args),
                }
            }
        handle = await start_ephemeral_workflow_for_agent(
            agent_id=agent_id,
            definition=definition,
            args=args,
            user_id=request.context.user_id,
            ledger_todo_id=ledger_todo_id,
            parent_session_id=request.context.session_id,
            root_session_id=request.context.session_id,
            definition_source="dynamic_workflow" if proposal_id or candidate_id else "ephemeral",
            run_metadata=run_metadata,
        )
    except (WorkflowCompileError, WorkflowAdmissionError, LookupError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    return json.dumps(
        {
            "ok": True,
            "run_id": str(handle.run_id),
            "status": handle.outcome.status,
            "reason": handle.outcome.reason,
        },
        ensure_ascii=False,
    )
