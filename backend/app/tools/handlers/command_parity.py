"""CC/Codex parity command tools.

These tools expose command-layer semantics to the model. They deliberately keep
TaskCreate cognitive-only: Work Ledger writes never start execution.
"""

from __future__ import annotations

import json
from typing import Any

from app.runtime.hooks import HookEvent, emit_hook
from app.services.agent_work_ledger import read_agent_work_ledger_view, upsert_agent_work_ledger_todo
from app.services.plan_verification_service import verify_plan_artifact
from app.services.runtime_task_service import get_runtime_task_record, update_runtime_task_record
from app.services.task_command_adapter import TaskCommandKind, adapt_task_command
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _session_id(request: ToolExecutionRequest) -> str | None:
    return str(request.context.session_id).strip() if request.context.session_id else None


def _can_access_runtime_task(record: dict[str, Any] | None, request: ToolExecutionRequest) -> bool:
    if not record:
        return False
    parent_agent_id = record.get("parent_agent_id")
    return parent_agent_id in {None, str(request.context.agent_id)}


@tool(
    ToolMeta(
        name="task_create",
        description=(
            "Create a cognitive task in your Work Ledger or a Team shared ledger. "
            "This never starts execution; use workflow/subagent/delegation tools for executable work."
        ),
        parameters={
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "owner": {"type": "string"},
                "team_id": {"type": "string"},
                "blocks": {"type": "array", "items": {"type": "string"}},
                "blockedBy": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["subject"],
        },
        category="command_task",
        display_name="Task Create",
        adapter="request",
    )
)
async def task_create(request: ToolExecutionRequest) -> str:
    plan = adapt_task_command("TaskCreate", request.arguments, current_session_id=_session_id(request))
    if plan.kind != TaskCommandKind.WORK_LEDGER_TODO:
        return _json({"ok": False, "error": plan.error or "Invalid task_create request."})
    payload = plan.work_ledger_payload
    result = upsert_agent_work_ledger_todo(
        agent_id=request.context.agent_id,
        title=payload.get("title"),
        description=request.arguments.get("description"),
        owner=payload.get("owner"),
        blocks=payload.get("blocks"),
        blocked_by=payload.get("blockedBy"),
        session_id=_session_id(request),
    )
    task = result["item"]
    await emit_hook(
        HookEvent.TASK_CREATED,
        agent_id=request.context.agent_id,
        session_id=_session_id(request),
        source="command_task",
        metadata={
            "tenant_id": request.context.tenant_id,
            "task_id": task.get("id"),
            "subject": task.get("title"),
            "owner": task.get("owner"),
            "starts_execution": False,
        },
    )
    return _json({"ok": True, "starts_execution": False, "task": result["item"]})


@tool(
    ToolMeta(
        name="task_update",
        description="Update a cognitive Work Ledger task. This never starts execution.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "subject": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                "owner": {"type": "string"},
            },
            "required": ["task_id"],
        },
        category="command_task",
        display_name="Task Update",
        adapter="request",
    )
)
async def task_update(request: ToolExecutionRequest) -> str:
    result = upsert_agent_work_ledger_todo(
        agent_id=request.context.agent_id,
        item_id=str(request.arguments.get("task_id") or ""),
        title=str(request.arguments.get("subject") or "").strip() or None,
        status=request.arguments.get("status"),
        owner=request.arguments.get("owner"),
        session_id=_session_id(request),
    )
    task = result["item"]
    if str(task.get("status") or "").strip() == "completed":
        await emit_hook(
            HookEvent.TASK_COMPLETED,
            agent_id=request.context.agent_id,
            session_id=_session_id(request),
            source="command_task",
            metadata={
                "tenant_id": request.context.tenant_id,
                "task_id": task.get("id"),
                "subject": task.get("title"),
                "owner": task.get("owner"),
                "starts_execution": False,
            },
        )
    return _json({"ok": True, "starts_execution": False, "task": result["item"]})


@tool(
    ToolMeta(
        name="task_list",
        description="List current cognitive Work Ledger tasks.",
        parameters={"type": "object", "properties": {}},
        category="command_task",
        display_name="Task List",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="request",
    )
)
async def task_list(request: ToolExecutionRequest) -> str:
    view = read_agent_work_ledger_view(agent_id=request.context.agent_id, session_id=_session_id(request))
    return _json({"ok": True, "tasks": (view or {}).get("todo_items", []), "ledger": view})


@tool(
    ToolMeta(
        name="task_get",
        description="Get one cognitive Work Ledger task by id.",
        parameters={"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        category="command_task",
        display_name="Task Get",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="request",
    )
)
async def task_get(request: ToolExecutionRequest) -> str:
    task_id = str(request.arguments.get("task_id") or "").strip()
    view = read_agent_work_ledger_view(agent_id=request.context.agent_id, session_id=_session_id(request))
    task = next((item for item in (view or {}).get("todo_items", []) if str(item.get("id")) == task_id), None)
    return _json({"ok": task is not None, "task": task, "error": "" if task else "Task not found."})


@tool(
    ToolMeta(
        name="task_output",
        description="Read output from an executable RuntimeTask. Requires runtime_task_id.",
        parameters={
            "type": "object",
            "properties": {"runtime_task_id": {"type": "string"}},
            "required": ["runtime_task_id"],
        },
        category="command_task",
        display_name="Task Output",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="request",
    )
)
async def task_output(request: ToolExecutionRequest) -> str:
    plan = adapt_task_command("TaskOutput", request.arguments, current_session_id=_session_id(request))
    if plan.kind == TaskCommandKind.INVALID:
        return _json({"ok": False, "error": plan.error})
    record = await get_runtime_task_record(plan.runtime_task_id or "")
    if record is None:
        return _json({"ok": False, "runtime_task_id": plan.runtime_task_id, "error": "RuntimeTask not found."})
    if not _can_access_runtime_task(record, request):
        return _json({"ok": False, "runtime_task_id": plan.runtime_task_id, "error": "forbidden"})
    return _json({"ok": True, "runtime_task_id": plan.runtime_task_id, "task": record})


@tool(
    ToolMeta(
        name="task_stop",
        description="Stop an executable RuntimeTask. Requires runtime_task_id.",
        parameters={
            "type": "object",
            "properties": {"runtime_task_id": {"type": "string"}},
            "required": ["runtime_task_id"],
        },
        category="command_task",
        display_name="Task Stop",
        adapter="request",
    )
)
async def task_stop(request: ToolExecutionRequest) -> str:
    plan = adapt_task_command("TaskStop", request.arguments, current_session_id=_session_id(request))
    if plan.kind == TaskCommandKind.INVALID:
        return _json({"ok": False, "error": plan.error})
    record = await get_runtime_task_record(plan.runtime_task_id or "")
    if record is None:
        return _json({"ok": False, "runtime_task_id": plan.runtime_task_id, "error": "RuntimeTask not found."})
    if not _can_access_runtime_task(record, request):
        return _json({"ok": False, "runtime_task_id": plan.runtime_task_id, "error": "forbidden"})
    if str(record.get("status") or "") in {"completed", "failed", "killed", "skipped", "needs_reconciliation"}:
        return _json(
            {
                "ok": True,
                "runtime_task_id": plan.runtime_task_id,
                "status": record.get("status"),
                "already_terminal": True,
            }
        )
    reason = str(request.arguments.get("reason") or "RuntimeTask cancelled by task_stop.").strip()
    await update_runtime_task_record(
        plan.runtime_task_id or "",
        status="killed",
        result_summary=reason,
        metadata_json={"cancel_reason": reason},
    )
    return _json({"ok": True, "runtime_task_id": plan.runtime_task_id, "status": "killed"})


@tool(
    ToolMeta(
        name="goal_start",
        description=(
            "Declare a session-scoped goal for bounded continuation. The durable goal is created "
            "through the session goals API so it remains tied to user/session permissions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "token_budget": {"type": "integer"},
                "max_continuation_turns": {"type": "integer"},
            },
            "required": ["objective"],
        },
        category="command_goal",
        display_name="Goal Start",
        adapter="request",
    )
)
async def goal_start(request: ToolExecutionRequest) -> str:
    objective = str(request.arguments.get("objective") or "").strip()
    if not objective:
        return _json({"ok": False, "error": "objective is required."})
    return _json(
        {
            "ok": True,
            "requires_api_persist": True,
            "session_id": _session_id(request),
            "objective": objective,
            "token_budget": request.arguments.get("token_budget"),
            "max_continuation_turns": request.arguments.get("max_continuation_turns"),
        }
    )


@tool(
    ToolMeta(
        name="team_create",
        description=(
            "Create an enterable Team workspace under the current session. The durable Team is "
            "created through the agent-teams API so member sessions remain permission-governed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "members": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["name", "members"],
        },
        category="command_team",
        display_name="Team Create",
        adapter="request",
    )
)
async def team_create(request: ToolExecutionRequest) -> str:
    name = str(request.arguments.get("name") or "").strip()
    members = request.arguments.get("members")
    if not name or not isinstance(members, list) or not members:
        return _json({"ok": False, "error": "name and non-empty members are required."})
    return _json(
        {
            "ok": True,
            "requires_api_persist": True,
            "parent_session_id": _session_id(request),
            "name": name,
            "members": members,
        }
    )


@tool(
    ToolMeta(
        name="advanced_plan",
        description=(
            "Start an advanced planning pass for the current session. This is the model-visible command "
            "handoff; the durable advanced_plan RuntimeTask is created through the advanced-plan API."
        ),
        parameters={
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "context": {"type": "object"},
            },
            "required": ["objective"],
        },
        category="command_plan",
        display_name="Advanced Plan",
        adapter="request",
    )
)
async def advanced_plan(request: ToolExecutionRequest) -> str:
    objective = str(request.arguments.get("objective") or "").strip()
    if not objective:
        return _json({"ok": False, "error": "objective is required."})
    context = request.arguments.get("context")
    return _json(
        {
            "ok": True,
            "requires_api_persist": True,
            "runtime_task_type": "advanced_plan",
            "session_id": _session_id(request),
            "objective": objective,
            "context": context if isinstance(context, dict) else {},
        }
    )


@tool(
    ToolMeta(
        name="verify_plan",
        description=(
            "Verify a plan artifact against its success criteria and explicit evidence references. "
            "This is an evidence check; it does not execute the plan."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan_json": {"type": "object"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "completed_criteria": {"type": "array", "items": {"type": "string"}},
                "failed_criteria": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": "string"},
            },
            "required": ["plan_json"],
        },
        category="command_plan",
        display_name="Verify Plan",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="request",
    )
)
async def verify_plan(request: ToolExecutionRequest) -> str:
    plan_json = request.arguments.get("plan_json")
    if not isinstance(plan_json, dict):
        return _json({"ok": False, "error": "plan_json is required."})
    verification = verify_plan_artifact(
        plan_json=plan_json,
        evidence_refs=request.arguments.get("evidence_refs")
        if isinstance(request.arguments.get("evidence_refs"), list)
        else [],
        completed_criteria=(
            request.arguments.get("completed_criteria")
            if isinstance(request.arguments.get("completed_criteria"), list)
            else []
        ),
        failed_criteria=(
            request.arguments.get("failed_criteria")
            if isinstance(request.arguments.get("failed_criteria"), list)
            else []
        ),
        notes=str(request.arguments.get("notes") or ""),
    )
    return _json({"ok": True, "verification": verification})
