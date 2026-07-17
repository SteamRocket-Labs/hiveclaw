"""Work Ledger tools — agent-authored cognitive scaffold (切口①).

docs/agent-task-cognitive-scaffold.md §5.2 Delta-1 / §5.4 / §5.6 / §7:

``track_todo`` / ``record_finding`` / ``read_ledger`` give the agent a CC-style
``TaskCreate`` / ``TaskUpdate`` / ``TaskList`` entry into the *existing* governed
Work Ledger. ``report_progress`` gives providers without a public commentary
channel an explicit model-authored Session progress surface. They are
**cognitive** actions, not governance actions:

* never ``governance="sensitive"`` and never tagged ``plan_gate_action_kind`` —
  writing a todo is a thought, not a request to act (the defining difference
  from DB Task / ``_manage_tasks`` REST helpers, which belong to the
  control-plane task record and execution boundary);
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


def _scope(request: ToolExecutionRequest) -> tuple[str | None, str | None, str | None]:
    """Optional plan/runtime-task scope; otherwise use the current chat session."""

    arguments = request.arguments
    plan_id = str(arguments.get("plan_id") or "").strip() or None
    runtime_task_id = str(arguments.get("runtime_task_id") or "").strip() or None
    raw_session_id = request.context.session_id
    session_id = None if plan_id or runtime_task_id or not raw_session_id else str(raw_session_id).strip() or None
    return plan_id, runtime_task_id, session_id


def _missing_scope_error() -> str:
    return _json(
        {
            "ok": False,
            "error_code": "missing_work_ledger_scope",
            "error": "Work Ledger writes require a session_id, plan_id, or runtime_task_id scope.",
        }
    )


def _has_write_scope(plan_id: str | None, runtime_task_id: str | None, session_id: str | None) -> bool:
    return bool(plan_id or runtime_task_id or session_id)


@tool(
    ToolMeta(
        name="report_progress",
        description=(
            "Publish one concise, model-authored progress update to the current Session. "
            "Use this before the first non-progress tool on multi-step work and again at "
            "meaningful milestones when your provider does not expose a public commentary "
            "channel. The `message` is user-visible public text: state observed progress, "
            "a decision, or the next action. Never put hidden chain-of-thought or "
            "provider-private reasoning here. This tool only reports progress; it does not "
            "start work, authorize an effect, or change a Task."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The concise public progress update, authored by you for the user.",
                },
            },
            "required": ["message"],
        },
        category="work_ledger",
        display_name="Report Progress",
        icon="💬",
        read_only=True,
        governance="safe",
        adapter="request",
    )
)
async def report_progress(request: ToolExecutionRequest) -> str:
    """Acknowledge public model text; the canonical tool event owns persistence.

    The runtime already commits the original tool arguments with stable event
    identity before the result is consumed. Keeping this handler semantic-free
    avoids a second prose source while still giving every provider an explicit
    public progress channel.
    """

    message = request.arguments.get("message")
    if not isinstance(message, str) or not message.strip():
        return _json(
            {
                "ok": False,
                "error_code": "missing_progress_message",
                "error": "`message` must contain the model-authored public progress update.",
            }
        )
    return _json({"ok": True, "acknowledged": True})


@tool(
    ToolMeta(
        name="track_todo",
        description=(
            "Maintain your own todo list for the current work — a lightweight cognitive "
            "scratchpad, like a notebook. Use this proactively on any task that takes more "
            "than a couple of steps: break the work into todos, then mark each one "
            "in_progress before you start it and completed when it is done.\n\n"
            "This is purely for organizing your own thinking — writing a todo does NOT start "
            "any execution, send anything, or commit to an action. To launch real "
            "background/autonomous work use `delegate_to_agent` / `spawn_subagent` / "
            "`start_workflow` instead.\n\n"
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
                "blocks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional ids of todos that cannot start until this one completes "
                        "(dependency note only — never triggers or orders execution)."
                    ),
                },
                "blockedBy": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional ids of todos that must complete before this one can start "
                        "(dependency note only — never triggers or orders execution)."
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
    plan_id, runtime_task_id, session_id = _scope(request)
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
    blocks = args.get("blocks")
    if blocks is not None and not isinstance(blocks, list):
        blocks = None
    blocked_by = args["blockedBy"] if "blockedBy" in args else args.get("blocked_by")
    if blocked_by is not None and not isinstance(blocked_by, list):
        blocked_by = None

    if action == "add":
        if not title:
            return _json({"ok": False, "error": "`title` is required to add a todo."})
    elif action == "update":
        if not item_id and not title:
            return _json({"ok": False, "error": "`item_id` is required to update a todo."})
    else:
        return _json({"ok": False, "error": "`action` must be 'add' or 'update'."})

    if not _has_write_scope(plan_id, runtime_task_id, session_id):
        return _missing_scope_error()

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
            blocks=[str(ref) for ref in blocks] if blocks is not None else None,
            blocked_by=[str(ref) for ref in blocked_by] if blocked_by is not None else None,
            plan_id=plan_id,
            runtime_task_id=runtime_task_id,
            session_id=session_id,
        )
    except KeyError:
        return _json({"ok": False, "error": f"Todo '{item_id}' not found in the current ledger."})

    return _json({"ok": True, "action": result["action"], "item": result["item"]})


@tool(
    ToolMeta(
        name="record_finding",
        description=(
            "Record a finding, an open question, a failed attempt, or a replan in your work ledger — "
            "a durable note to yourself so you don't lose what you learned or repeat a dead "
            "end after a context reset. Cognitive only: recording a note never starts any "
            "execution or external action.\n\n"
            "Usage:\n"
            "- type='finding': something you verified or concluded (use `trust` and `source_refs`).\n"
            "- type='open_question': an unresolved question to come back to.\n"
            "- type='failure': an attempt that failed (use `next_strategy` for what to try instead).\n"
            "- type='replan': a deliberate strategy change after stall/failure "
            "(use `next_strategy` for the changed approach)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["finding", "open_question", "failure", "replan"],
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
                    "description": "For a failure or replan: what to try differently next time.",
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
    plan_id, runtime_task_id, session_id = _scope(request)

    if finding_type not in {"finding", "open_question", "failure", "replan"}:
        return _json({"ok": False, "error": "`type` must be 'finding', 'open_question', 'failure', or 'replan'."})
    if not summary:
        return _json({"ok": False, "error": "`summary` is required."})
    if not _has_write_scope(plan_id, runtime_task_id, session_id):
        return _missing_scope_error()

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
            session_id=session_id,
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
    plan_id, runtime_task_id, session_id = _scope(request)
    view = read_agent_work_ledger_view(
        agent_id=request.context.agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
    )
    return _json({"ok": True, "ledger": view})
