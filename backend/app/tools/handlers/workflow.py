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
) -> str:
    _prune_preview_cache()
    preview_id = str(uuid.uuid4())
    _WORKFLOW_PREVIEW_CACHE[preview_id] = {
        "agent_id": str(agent_id),
        "definition_hash": definition_hash,
        "args_hash": args_hash,
        "confirmation_required": confirmation_required,
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
            ),
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
async def start_workflow(agent_id: uuid.UUID, arguments: dict) -> str:
    definition = arguments.get("definition") or {}
    args = arguments.get("args") or {}
    ledger_todo_id = arguments.get("ledger_todo_id") or None
    preview_id = str(arguments.get("preview_id") or "").strip() or None
    definition_hash = str(arguments.get("definition_hash") or "").strip() or None
    args_hash = str(arguments.get("args_hash") or "").strip() or None
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
        handle = await start_ephemeral_workflow_for_agent(
            agent_id=agent_id,
            definition=definition,
            args=args,
            ledger_todo_id=ledger_todo_id,
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
