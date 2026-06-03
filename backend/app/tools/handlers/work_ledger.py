"""Work Ledger tools — agent-authored cognitive scaffold (切口①).

docs/agent-task-cognitive-scaffold.md §5.2 Delta-1 / §5.4 / §5.6 / §7:

``track_todo`` / ``record_finding`` / ``read_ledger`` give the agent a CC-style
``TaskCreate`` / ``TaskUpdate`` / ``TaskList`` entry into the *existing* governed
Work Ledger. They are **cognitive** actions, not governance actions:

* never ``governance="sensitive"`` and never tagged ``plan_gate_action_kind`` —
  writing a todo is a thought, not a request to act (the defining difference
  from ``manage_tasks``, which is sensitive + ``start_long_task`` + create-即-
  execute);
* writing the ledger **never triggers execution** (§5.4 Delta-3, §8 invariant 1);
* untrusted text in ``title`` / ``summary`` is stored as ledger *data*, never
  interpreted as instruction (§8 invariant 3) — the service persists them as
  plain JSON string values;
* scope is the calling agent (read from ``ToolExecutionContext``); the service
  keys every artifact under ``AGENT_DATA_DIR/<agent_id>/`` so one agent can
  never read or overwrite another's ledger (§8 invariant 5).

The handler is a thin shell over the service write/read primitives.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.services.agent_work_ledger import (
    append_agent_work_ledger_finding,
    read_agent_work_ledger_view,
    upsert_agent_work_ledger_todo,
)
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _scope(arguments: dict) -> tuple[str | None, str | None]:
    """Optional plan/runtime-task scope (general path leaves both unset)."""

    plan_id = str(arguments.get("plan_id") or "").strip() or None
    runtime_task_id = str(arguments.get("runtime_task_id") or "").strip() or None
    return plan_id, runtime_task_id


@tool(
    ToolMeta(
        name="track_todo",
        description=(
            "Maintain your own todo list for the current work — a lightweight cognitive "
            "scratchpad, like a notebook. Use this proactively on any task that takes more "
            "than a couple of steps: break the work into todos, then mark each one "
            "in_progress before you start it and completed when it is done.\n\n"
            "This is purely for organizing your own thinking — writing a todo does NOT start "
            "any execution, send anything, or commit to an action. To actually launch an "
            "autonomous job use `manage_tasks` instead.\n\n"
            "Usage:\n"
            "- action='add': create a new todo (requires `title`).\n"
            "- action='update': change an existing todo by `item_id` (set `status` to "
            "pending/in_progress/completed, or refine title/description/activeForm)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "update"],
                    "description": "Add a new todo or update an existing one.",
                },
                "item_id": {
                    "type": "string",
                    "description": "Id of the todo to update (returned when it was added). Required for update.",
                },
                "title": {"type": "string", "description": "Short description of the todo. Required for add."},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                    "description": "Todo status. Mark in_progress when you start it, completed when done.",
                },
                "description": {"type": "string", "description": "Optional longer detail for the todo."},
                "activeForm": {
                    "type": "string",
                    "description": "Optional present-tense form shown while in progress, e.g. 'Collecting Q3 figures'.",
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional file paths or links that evidence this todo.",
                },
                "owner": {
                    "type": "string",
                    "description": (
                        "Optional id or name of who this todo is assigned to — e.g. another agent "
                        "you are delegating it to. Setting an owner is just a note; it does NOT start "
                        "or delegate any work on its own."
                    ),
                },
            },
            "required": ["action"],
        },
        category="work_ledger",
        display_name="Track Todo",
        icon="☑️",
        adapter="request",
    )
)
async def track_todo(request: ToolExecutionRequest) -> str:
    args = request.arguments
    action = str(args.get("action") or "").strip().lower()
    plan_id, runtime_task_id = _scope(args)
    agent_id: uuid.UUID = request.context.agent_id

    title = str(args.get("title") or "").strip()
    item_id = str(args.get("item_id") or "").strip() or None
    status = args.get("status")
    status = str(status).strip() if status is not None else None
    description = args.get("description")
    active_form = args.get("activeForm") or args.get("active_form")
    evidence_refs = args.get("evidence_refs")
    if evidence_refs is not None and not isinstance(evidence_refs, list):
        evidence_refs = None
    owner = args.get("owner")

    if action == "add":
        if not title:
            return _json({"ok": False, "error": "`title` is required to add a todo."})
    elif action == "update":
        if not item_id and not title:
            return _json({"ok": False, "error": "`item_id` is required to update a todo."})
    else:
        return _json({"ok": False, "error": "`action` must be 'add' or 'update'."})

    try:
        result = upsert_agent_work_ledger_todo(
            agent_id=agent_id,
            item_id=item_id,
            title=title or None,
            status=status,
            description=str(description).strip() if description is not None else None,
            active_form=str(active_form).strip() if active_form is not None else None,
            evidence_refs=[str(ref) for ref in evidence_refs] if evidence_refs is not None else None,
            owner=str(owner).strip() if owner is not None else None,
            plan_id=plan_id,
            runtime_task_id=runtime_task_id,
        )
    except KeyError:
        return _json({"ok": False, "error": f"Todo '{item_id}' not found in the current ledger."})

    return _json({"ok": True, "action": result["action"], "item": result["item"]})


@tool(
    ToolMeta(
        name="record_finding",
        description=(
            "Record a finding, an open question, or a failed attempt in your work ledger — "
            "a durable note to yourself so you don't lose what you learned or repeat a dead "
            "end after a context reset. Cognitive only: recording a note never starts any "
            "execution or external action.\n\n"
            "Usage:\n"
            "- type='finding': something you verified or concluded (use `trust` and `source_refs`).\n"
            "- type='open_question': an unresolved question to come back to.\n"
            "- type='failure': an attempt that failed (use `next_strategy` for what to try instead)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["finding", "open_question", "failure"],
                    "description": "Which kind of note this is.",
                },
                "summary": {"type": "string", "description": "The finding / question / failure, in one line."},
                "source_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional evidence paths or links (for findings).",
                },
                "trust": {
                    "type": "string",
                    "description": "Optional trust level for a finding, e.g. 'verified' or 'unverified'.",
                },
                "next_strategy": {
                    "type": "string",
                    "description": "For a failure: what to try differently next time.",
                },
            },
            "required": ["type", "summary"],
        },
        category="work_ledger",
        display_name="Record Finding",
        icon="\U0001f4dd",
        adapter="request",
    )
)
async def record_finding(request: ToolExecutionRequest) -> str:
    args = request.arguments
    finding_type = str(args.get("type") or "").strip().lower()
    summary = str(args.get("summary") or "").strip()
    plan_id, runtime_task_id = _scope(args)

    if finding_type not in {"finding", "open_question", "failure"}:
        return _json({"ok": False, "error": "`type` must be 'finding', 'open_question', or 'failure'."})
    if not summary:
        return _json({"ok": False, "error": "`summary` is required."})

    source_refs = args.get("source_refs")
    if source_refs is not None and not isinstance(source_refs, list):
        source_refs = None
    trust = args.get("trust")
    next_strategy = args.get("next_strategy")

    try:
        result = append_agent_work_ledger_finding(
            agent_id=request.context.agent_id,
            finding_type=finding_type,
            summary=summary,
            source_refs=[str(ref) for ref in source_refs] if source_refs is not None else None,
            trust=str(trust).strip() if trust is not None else None,
            next_strategy=str(next_strategy).strip() if next_strategy is not None else None,
            plan_id=plan_id,
            runtime_task_id=runtime_task_id,
        )
    except ValueError as exc:
        return _json({"ok": False, "error": f"Could not record finding: {exc}"})

    return _json({"ok": True, "type": result["type"], "recorded": result["recorded"]})


@tool(
    ToolMeta(
        name="read_ledger",
        description=(
            "Read back your current work ledger — phase, todos, findings, open questions, and "
            "failures. Use this to recover your bearings after a context reset or when resuming "
            "a partially finished task: check what is still open and which dead ends to avoid "
            "before deciding the next step."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "Optional plan scope to read."},
                "runtime_task_id": {"type": "string", "description": "Optional runtime-task scope to read."},
            },
        },
        category="work_ledger",
        display_name="Read Ledger",
        icon="\U0001f4d6",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="request",
    )
)
async def read_ledger(request: ToolExecutionRequest) -> str:
    plan_id, runtime_task_id = _scope(request.arguments)
    view = read_agent_work_ledger_view(
        agent_id=request.context.agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
    )
    return _json({"ok": True, "ledger": view})
