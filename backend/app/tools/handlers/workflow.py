"""Workflow tools (§9 P4): preview + start for ephemeral definitions.

The agent submits DEFINITION DATA only (§3.2) — the engine interprets it;
there is no code execution surface. ``start_workflow`` is risk-graded
through the standard plan-gate intercept (``ToolMeta.plan_gate_action_kind``
+ the argument-aware classifier in plan_gate_registry): low-risk launches
run directly (equivalent governance to the agent calling its own tools in
sequence), high-risk launches are hard-gated on a confirmed plan BEFORE this
handler ever runs.
"""

from __future__ import annotations

import json
import uuid

from app.runtime.workflow_admission import AdmissionLimits, WorkflowAdmissionError, admit_workflow
from app.runtime.workflow_compiler import WorkflowCompileError, compile_workflow
from app.services.workflow_launch import classify_workflow_risk, start_ephemeral_workflow_for_agent
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
            "- Always preview before start_workflow: returns definition_hash, risk level "
            "(low/high), planned leaf calls and budget.\n"
            "- High-risk workflows (external/irreversible effects, big budget/fanout/waits) "
            "need a confirmed plan before start_workflow will run.\n"
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
        risk = classify_workflow_risk(compiled, args=args)
    except (WorkflowCompileError, WorkflowAdmissionError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    return json.dumps(
        {
            "ok": True,
            "definition_hash": compiled.definition_hash,
            "risk": risk.level,
            "risk_reasons": risk.reasons,
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
            "Usage:\n"
            "- preview_workflow FIRST and show the user what will run.\n"
            "- Low-risk (read-only/workspace, small budget) runs start directly.\n"
            "- High-risk (external/irreversible effects, big budget/fanout/waits) requires a "
            "confirmed plan — create one via Plan Mode and retry after confirmation.\n"
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
        plan_gate_action_kind="start_workflow",
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
