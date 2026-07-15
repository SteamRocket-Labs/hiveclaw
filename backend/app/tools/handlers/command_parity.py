"""CC/Codex parity command tools.

These tools expose command-layer semantics to the model. They deliberately keep
TaskCreate cognitive-only: Work Ledger writes never start execution.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from app.runtime.prompts.command_parity import (
    ADVANCED_PLAN_DESCRIPTION,
    GET_GOAL_DESCRIPTION,
    GOAL_START_DESCRIPTION,
    TASK_CREATE_DESCRIPTION,
    TASK_UPDATE_DESCRIPTION,
    TEAM_CREATE_DESCRIPTION,
    UPDATE_GOAL_DESCRIPTION,
    VERIFY_PLAN_DESCRIPTION,
)
from app.runtime.hooks import HookEvent, emit_hook
from app.services.agent_team_runtime_service import create_agent_team_from_tool_request
from app.services.agent_work_ledger import read_agent_work_ledger_view, upsert_agent_work_ledger_todo
from app.services.command_escalation_service import request_command_escalation
from app.services.plan_verification_service import verify_plan_artifact
from app.services.runtime_task_service import get_runtime_task_record, update_runtime_task_record
from app.services.runtime_task_authority import (
    authorize_runtime_task_record,
    execution_principal_from_tool_context,
)
from app.services.session_goal_service import (
    get_session_goal_from_tool,
    persist_session_goal_from_tool,
    update_session_goal_from_tool,
)
from app.services.task_command_adapter import TaskCommandKind, adapt_task_command
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _session_id(request: ToolExecutionRequest) -> str | None:
    return str(request.context.session_id).strip() if request.context.session_id else None


@tool(
    ToolMeta(
        name="task_create",
        description=TASK_CREATE_DESCRIPTION,
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
        description=TASK_UPDATE_DESCRIPTION,
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
    authority = authorize_runtime_task_record(
        record,
        principal=execution_principal_from_tool_context(request.context),
        action="read_output",
    )
    if not authority.allowed:
        return _json(
            {
                "ok": False,
                "runtime_task_id": plan.runtime_task_id,
                "error": "forbidden",
                "reason": authority.reason,
            }
        )
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
    authority = authorize_runtime_task_record(
        record,
        principal=execution_principal_from_tool_context(request.context),
        action="stop",
    )
    if not authority.allowed:
        return _json(
            {
                "ok": False,
                "runtime_task_id": plan.runtime_task_id,
                "error": "forbidden",
                "reason": authority.reason,
            }
        )
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
    if str(record.get("task_type") or "") == "subagent":
        try:
            from app.services.runtime_control_bus import publish_subagent_cancel

            await publish_subagent_cancel(
                run_id=plan.runtime_task_id or "",
                parent_agent_id=record.get("parent_agent_id"),
            )
        except Exception as exc:  # noqa: BLE001 - persisted killed state remains the fallback contract.
            logger.debug("Failed to publish subagent cancellation {}: {}", plan.runtime_task_id, exc)
    return _json({"ok": True, "runtime_task_id": plan.runtime_task_id, "status": "killed"})


@tool(
    ToolMeta(
        name="goal_start",
        description=GOAL_START_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "token_budget": {"type": "integer"},
                "max_continuation_turns": {"type": "integer"},
                "attention_set": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Topics, entities, or files most relevant to this goal. "
                        "They stay warm in recall for the whole goal — declare "
                        "the handful of things you expect to keep coming back to."
                    ),
                },
            },
            "required": ["objective"],
        },
        category="command_goal",
        display_name="Goal Start",
        work_amplifying=True,
        adapter="request",
    )
)
async def goal_start(request: ToolExecutionRequest) -> str:
    objective = str(request.arguments.get("objective") or "").strip()
    if not objective:
        return _json({"ok": False, "error": "objective is required."})
    session_id = _session_id(request)
    if not session_id:
        return _json({"ok": False, "error": "a session is required to start a goal."})
    result = await persist_session_goal_from_tool(
        agent_id=request.context.agent_id,
        session_id=session_id,
        user_id=request.context.user_id,
        objective=objective,
        token_budget=request.arguments.get("token_budget"),
        max_continuation_turns=request.arguments.get("max_continuation_turns"),
        attention_set=_string_list_argument(request.arguments.get("attention_set")),
    )
    # A9: the goal is persisted here now, so the API layer no longer needs to
    # re-persist it. Keep the field for frontend compatibility, set to False.
    return _json({"requires_api_persist": False, "session_id": session_id, **result})


@tool(
    ToolMeta(
        name="update_goal",
        description=UPDATE_GOAL_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["complete", "blocked", "paused", "active"]},
                "objective": {"type": "string"},
                "summary": {"type": "string"},
                "attention_set": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Refresh the goal's warm recall set (topics/entities/files). "
                        "A new declaration replaces the previous one; terminal "
                        "statuses release it automatically."
                    ),
                },
            },
        },
        category="command_goal",
        display_name="Update Goal",
        work_amplifying=True,
        work_amplifying_safe_when="goal_nonactivating",
        adapter="request",
    )
)
async def update_goal(request: ToolExecutionRequest) -> str:
    session_id = _session_id(request)
    if not session_id:
        return _json({"ok": False, "error": "a session is required to update a goal."})
    result = await update_session_goal_from_tool(
        agent_id=request.context.agent_id,
        session_id=session_id,
        status=request.arguments.get("status"),
        objective=request.arguments.get("objective"),
        summary=request.arguments.get("summary"),
        attention_set=_string_list_argument(request.arguments.get("attention_set")),
    )
    return _json(result)


def _string_list_argument(value: Any) -> list[str] | None:
    if not isinstance(value, list | tuple):
        return None
    cleaned = [text for item in value if (text := str(item or "").strip())]
    return cleaned or None


@tool(
    ToolMeta(
        name="get_goal",
        description=GET_GOAL_DESCRIPTION,
        parameters={"type": "object", "properties": {}},
        category="command_goal",
        display_name="Get Goal",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="request",
    )
)
async def get_goal(request: ToolExecutionRequest) -> str:
    session_id = _session_id(request)
    if not session_id:
        return _json({"ok": False, "error": "a session is required to read a goal."})
    result = await get_session_goal_from_tool(
        agent_id=request.context.agent_id,
        session_id=session_id,
    )
    return _json(result)


_REQUEST_SHELL_ESCALATION_DESCRIPTION = (
    "Request one-time human approval to run a shell command that the execution "
    "gate blocked (a dangerous or sandbox-denied command). Provide the EXACT "
    "command you need and a short reason. This does NOT run the command now: it "
    "creates an approval for an admin/creator to review. If they approve, that "
    "exact command runs once. Nothing is remembered — running it again later "
    "requires a new escalation request. Only escalate when the blocked command is "
    "genuinely necessary; prefer a safer command first."
)


@tool(
    ToolMeta(
        name="request_shell_escalation",
        description=_REQUEST_SHELL_ESCALATION_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The exact shell command to escalate (verbatim, as you would pass to run_command).",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this specific command is needed; shown to the approver.",
                },
            },
            "required": ["command"],
        },
        category="command_exec",
        display_name="Request Shell Escalation",
        adapter="request",
    )
)
async def request_shell_escalation(request: ToolExecutionRequest) -> str:
    command = str(request.arguments.get("command") or "").strip()
    if not command:
        return _json({"ok": False, "error": "command is required."})
    reason = str(request.arguments.get("reason") or "").strip()
    outcome = await request_command_escalation(
        agent_id=request.context.agent_id,
        tenant_id=request.context.tenant_id,
        requested_by=request.context.user_id,
        command=command,
        reason=reason,
        session_id=_session_id(request),
    )
    has_error = "error" in outcome
    return _json(
        {
            "ok": not has_error,
            "status": "error" if has_error else "approval_required",
            "command": command,
            "escalation": outcome,
            "note": (
                "A human must approve this exact command; on approval it runs once and is not remembered. "
                "Re-running it later needs a new escalation request."
            ),
        }
    )


@tool(
    ToolMeta(
        name="team_create",
        timeout_seconds=60.0,
        risk_class="side_effect_governed",
        idempotency_scope="tool_call",
        description=TEAM_CREATE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "team_name": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "agent_type": {"type": "string"},
            },
            "anyOf": [{"required": ["team_name"]}, {"required": ["name"]}],
        },
        category="command_team",
        display_name="Team Create",
        adapter="request",
    )
)
async def team_create(request: ToolExecutionRequest) -> str:
    name = str(request.arguments.get("team_name") or request.arguments.get("name") or "").strip()
    if not name:
        return _json({"ok": False, "error": "team_name is required."})
    try:
        return _json(
            await create_agent_team_from_tool_request(
                request,
                name=name,
                members=None,
                description=str(request.arguments.get("description") or "").strip(),
                agent_type=str(request.arguments.get("agent_type") or "").strip(),
            )
        )
    except Exception as exc:
        return _json({"ok": False, "error": f"team_create failed: {exc}"})


@tool(
    ToolMeta(
        name="advanced_plan",
        description=ADVANCED_PLAN_DESCRIPTION,
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
        description=VERIFY_PLAN_DESCRIPTION,
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
