"""Workflow tools: preview + start for ephemeral definitions.

The agent submits DEFINITION DATA only (§3.2) — the engine interprets it;
there is no code execution surface. ``preview_workflow`` surfaces deterministic
confirmation notes, but ``start_workflow`` never enters Plan Mode automatically.
"""

from __future__ import annotations

import json
import uuid

from app.runtime.workflow_admission import AdmissionLimits, WorkflowAdmissionError, admit_workflow
from app.runtime.workflow_compiler import WorkflowCompileError, compile_workflow
from app.services.workflow_launch import inspect_workflow_confirmation_needs, start_ephemeral_workflow_for_agent
from app.tools.decorator import ToolMeta, tool

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
            "- Always preview before start_workflow: returns definition_hash, confirmation notes, "
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
            "definition_hash": compiled.definition_hash,
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
    try:
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
