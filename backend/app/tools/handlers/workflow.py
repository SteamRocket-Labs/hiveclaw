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

from app.runtime.dynamic_workflow import (
    build_dynamic_workflow_run_metadata,
    dump_json,
    mapping,
    validate_dynamic_workflow_proposal,
)
from app.runtime.context_budget import build_tool_execution_shape_decision, execution_shape_from_round_state
from app.runtime.workflow_admission import AdmissionLimits, WorkflowAdmissionError, admit_workflow, normalize_workflow_args
from app.runtime.workflow_compiler import WorkflowCompileError, compile_workflow
from app.runtime.workflow_definition import compute_definition_hash
from app.runtime.workflow_preview import (
    get_workflow_preview,
    prune_workflow_preview_cache,
    record_workflow_preview,
    validate_workflow_preview_binding,
)
from app.services.workflow_launch import inspect_workflow_confirmation_needs, start_ephemeral_workflow_for_agent
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest

_PREVIEW_TTL_SECONDS = 60 * 60
_DYNAMIC_WORKFLOW_PROPOSAL_CACHE: dict[str, dict[str, Any]] = {}


_DEFINITION_PARAM = {
    "type": "object",
    "description": (
        "Structured workflow definition (data, not code): name, args_schema, steps "
        "(agent_step/fanout_step/gate_step/wait_until_step). Task strings may reference "
        "{{args.x}} and {{steps.<id>.output}} (pure key substitution)."
    ),
}


def _cache_dynamic_workflow_proposal(proposal: dict[str, Any]) -> None:
    proposal_id = str(proposal.get("proposal_id") or "").strip()
    if not proposal_id:
        return
    _DYNAMIC_WORKFLOW_PROPOSAL_CACHE[proposal_id] = {
        **proposal,
        "created_at": time.monotonic(),
    }


def _load_dynamic_candidate(proposal_id: str | None, candidate_id: str | None) -> dict[str, Any] | None:
    if not proposal_id or not candidate_id:
        return None
    prune_workflow_preview_cache()
    proposal = _DYNAMIC_WORKFLOW_PROPOSAL_CACHE.get(proposal_id)
    if not proposal:
        return None
    if time.monotonic() - float(proposal.get("created_at", 0.0)) > _PREVIEW_TTL_SECONDS:
        _DYNAMIC_WORKFLOW_PROPOSAL_CACHE.pop(proposal_id, None)
        return None
    for candidate in proposal.get("candidates") or []:
        if isinstance(candidate, dict) and candidate.get("candidate_id") == candidate_id:
            return candidate
    return None


def _dynamic_candidate_binding_error(
    *,
    proposal_id: str | None,
    candidate_id: str | None,
    dynamic_candidate: dict[str, Any] | None,
    definition_hash: str,
    args_hash: str,
) -> str | None:
    if not proposal_id and not candidate_id:
        return None
    if not proposal_id or not candidate_id:
        return "Dynamic Workflow preview requires both proposal_id and candidate_id"
    if not dynamic_candidate:
        return "Dynamic Workflow candidate was not found or expired; call propose_dynamic_workflow again"
    if dynamic_candidate.get("definition_hash") != definition_hash:
        return "Dynamic Workflow candidate lowered_definition differs from the preview_workflow definition"
    if dynamic_candidate.get("args_hash") != args_hash:
        return "Dynamic Workflow candidate preview_args differ from the preview_workflow args"
    return None


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
        adapter="agent_args",
    )
)
async def propose_dynamic_workflow(agent_id: uuid.UUID, arguments: dict) -> str:
    _ = agent_id
    proposal = validate_dynamic_workflow_proposal(arguments)
    if proposal.get("ok") is True:
        _cache_dynamic_workflow_proposal(proposal)
    return dump_json(proposal)


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
                "proposal_id": {
                    "type": "string",
                    "description": "Dynamic Workflow proposal_id, if previewing a candidate.",
                },
                "candidate_id": {
                    "type": "string",
                    "description": "Dynamic Workflow candidate_id, if previewing a candidate.",
                },
            },
            "required": ["definition"],
        },
        category="workflow",
        display_name="Preview Workflow",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="agent_args",
    )
)
async def preview_workflow(agent_id: uuid.UUID, arguments: dict) -> str:
    definition = arguments.get("definition") or {}
    args = arguments.get("args") or {}
    proposal_id = str(arguments.get("proposal_id") or "").strip() or None
    candidate_id = str(arguments.get("candidate_id") or "").strip() or None
    dynamic_candidate = _load_dynamic_candidate(proposal_id, candidate_id)
    try:
        compiled = compile_workflow(definition)
        args = normalize_workflow_args(compiled, args)
        from app.config import get_settings

        admission = admit_workflow(compiled, args=args, limits=AdmissionLimits.from_settings(get_settings()))
        confirmation = inspect_workflow_confirmation_needs(compiled, args=args)
    except (WorkflowCompileError, WorkflowAdmissionError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    args_hash = compute_definition_hash(args)
    dynamic_error = _dynamic_candidate_binding_error(
        proposal_id=proposal_id,
        candidate_id=candidate_id,
        dynamic_candidate=dynamic_candidate,
        definition_hash=compiled.definition_hash,
        args_hash=args_hash,
    )
    if dynamic_error:
        return json.dumps({"ok": False, "error": dynamic_error}, ensure_ascii=False)

    return json.dumps(
        {
            "ok": True,
            "preview_id": record_workflow_preview(
                agent_id=agent_id,
                definition_hash=compiled.definition_hash,
                args_hash=args_hash,
                confirmation_required=confirmation.requires_confirmation,
                proposal_id=proposal_id,
                candidate_id=candidate_id,
                dynamic_candidate=dynamic_candidate,
            ),
            "proposal_id": proposal_id,
            "candidate_id": candidate_id,
            "definition_hash": compiled.definition_hash,
            "args_hash": args_hash,
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
                "proposal_id": {
                    "type": "string",
                    "description": "Dynamic Workflow proposal_id bound by preview_workflow.",
                },
                "candidate_id": {
                    "type": "string",
                    "description": "Dynamic Workflow candidate_id bound by preview_workflow.",
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
    session_id = request.context.session_id
    execution_shape_decision = build_tool_execution_shape_decision(
        "start_workflow",
        execution_shape_from_round_state(request.context.round_state),
    )
    if not session_id:
        return json.dumps(
            {
                "ok": False,
                "error_code": "missing_workflow_session",
                "error": (
                    "start_workflow must run inside the current chat session so the Dynamic Workflow run "
                    "can be resumed and shown in the session workbench."
                ),
            },
            ensure_ascii=False,
        )
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
        compiled = compile_workflow(definition)
        args = normalize_workflow_args(compiled, args)
        preview_ok, preview_error, preview_record = validate_workflow_preview_binding(
            agent_id=agent_id,
            definition=definition,
            args=args,
            preview_id=preview_id,
            expected_definition_hash=definition_hash,
            expected_args_hash=args_hash,
            allow_hash_fallback=True,
        )
        if not preview_ok:
            return json.dumps({"ok": False, "error": preview_error}, ensure_ascii=False)
        preview_record = preview_record or get_workflow_preview(preview_id)
        preview_proposal_id = str((preview_record or {}).get("proposal_id") or "").strip() or None
        preview_candidate_id = str((preview_record or {}).get("candidate_id") or "").strip() or None
        if (proposal_id or candidate_id) and not (preview_proposal_id and preview_candidate_id):
            return json.dumps(
                {
                    "ok": False,
                    "error": "start_workflow dynamic identifiers require preview_workflow with the same proposal_id and candidate_id",
                },
                ensure_ascii=False,
            )
        if proposal_id and preview_proposal_id and proposal_id != preview_proposal_id:
            return json.dumps(
                {"ok": False, "error": "start_workflow proposal_id differs from preview_workflow"}, ensure_ascii=False
            )
        if candidate_id and preview_candidate_id and candidate_id != preview_candidate_id:
            return json.dumps(
                {"ok": False, "error": "start_workflow candidate_id differs from preview_workflow"}, ensure_ascii=False
            )
        proposal_id = proposal_id or preview_proposal_id
        candidate_id = candidate_id or preview_candidate_id
        dynamic_candidate = mapping((preview_record or {}).get("dynamic_candidate"))
        run_metadata = build_dynamic_workflow_run_metadata(
            proposal_id=proposal_id,
            candidate_id=candidate_id,
            preview_id=preview_id,
            definition_hash=compiled.definition_hash,
            args_hash=compute_definition_hash(args),
            candidate=dynamic_candidate,
        )
        if run_metadata is None:
            run_metadata = {}
        run_metadata["execution_shape_decision"] = execution_shape_decision
        handle = await start_ephemeral_workflow_for_agent(
            agent_id=agent_id,
            definition=definition,
            args=args,
            user_id=request.context.user_id,
            ledger_todo_id=ledger_todo_id,
            parent_session_id=session_id,
            root_session_id=session_id,
            definition_source="dynamic_workflow" if proposal_id or candidate_id else "ephemeral",
            run_metadata=run_metadata,
            enqueue_only=True,
            budget_run_id=request.context.budget_run_id,
        )
    except (WorkflowCompileError, WorkflowAdmissionError, LookupError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    return json.dumps(
        {
            "ok": True,
            "run_id": str(handle.run_id),
            "status": handle.outcome.status,
            "reason": handle.outcome.reason,
            "execution_shape_decision": execution_shape_decision,
        },
        ensure_ascii=False,
    )
