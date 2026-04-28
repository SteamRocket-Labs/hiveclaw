"""Task tools — DB-backed task read/write helpers."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select

from app.database import async_session
from app.models.task import Task, TaskLog
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest


def _task_line(task: Task) -> str:
    due = f", due={task.due_date.isoformat()}" if task.due_date else ""
    return f"- {task.title} [{task.status}/{task.priority}/{task.type}] id={task.id}{due}"


@tool(
    ToolMeta(
        name="list_tasks",
        description=(
            "List this agent's DB-backed tasks. Use this for a compact task ledger view; "
            "use `manage_tasks` to create, update, or delete tasks."
        ),
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Optional status filter: pending, doing, or done.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of tasks to return (default 20, max 100).",
                },
            },
        },
        category="tasks",
        display_name="List Tasks",
        icon="\U0001f4cb",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="agent_args",
    )
)
async def list_tasks(agent_id: uuid.UUID, arguments: dict) -> str:
    limit = arguments.get("limit", 20)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    status = str(arguments.get("status") or "").strip()

    async with async_session() as db:
        query = select(Task).where(Task.agent_id == agent_id).order_by(Task.created_at.desc()).limit(limit)
        if status:
            query = (
                select(Task)
                .where(Task.agent_id == agent_id, Task.status == status)
                .order_by(Task.created_at.desc())
                .limit(limit)
            )
        result = await db.execute(query)
        tasks = result.scalars().all()

    if not tasks:
        suffix = f" with status '{status}'" if status else ""
        return f"No tasks found{suffix}."
    return "Tasks:\n" + "\n".join(_task_line(task) for task in tasks)


@tool(
    ToolMeta(
        name="get_task",
        description="Read one DB-backed task by id or title substring, including recent progress logs.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Exact task UUID."},
                "title": {"type": "string", "description": "Task title or title substring."},
            },
        },
        category="tasks",
        display_name="Get Task",
        icon="\U0001f50e",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="agent_args",
    )
)
async def get_task(agent_id: uuid.UUID, arguments: dict) -> str:
    task_id_raw = str(arguments.get("task_id") or "").strip()
    title = str(arguments.get("title") or "").strip()
    if not task_id_raw and not title:
        return "Provide `task_id` or `title`."

    async with async_session() as db:
        if task_id_raw:
            try:
                task_id = uuid.UUID(task_id_raw)
            except ValueError:
                return f"Invalid task_id: {task_id_raw}"
            task_result = await db.execute(select(Task).where(Task.agent_id == agent_id, Task.id == task_id))
        else:
            task_result = await db.execute(
                select(Task)
                .where(Task.agent_id == agent_id, Task.title.ilike(f"%{title}%"))
                .order_by(Task.created_at.desc())
                .limit(1)
            )
        task = task_result.scalars().first()
        if not task:
            return "Task not found."

        log_result = await db.execute(
            select(TaskLog).where(TaskLog.task_id == task.id).order_by(TaskLog.created_at.desc()).limit(5)
        )
        logs = log_result.scalars().all()

    parts = [
        f"Task: {task.title}",
        f"- id: {task.id}",
        f"- status: {task.status}",
        f"- priority: {task.priority}",
        f"- type: {task.type}",
    ]
    if task.description:
        parts.append(f"- description: {task.description}")
    if task.due_date:
        parts.append(f"- due_date: {task.due_date.isoformat()}")
    if logs:
        parts.append("- recent_logs:")
        parts.extend(f"  - {log.created_at.isoformat()}: {log.content}" for log in logs)
    return "\n".join(parts)


@tool(
    ToolMeta(
        name="manage_tasks",
        description=(
            "Create, update, or delete DB-backed tasks for this agent. "
            "This also syncs tasks.json. Protected tasks.json should not be edited directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update_status", "delete"],
                    "description": "Task operation.",
                },
                "title": {"type": "string", "description": "Task title or title substring."},
                "description": {"type": "string", "description": "Task description for create."},
                "task_type": {
                    "type": "string",
                    "enum": ["todo", "supervision"],
                    "description": "Task type for create.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "urgent"],
                    "description": "Priority for create.",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "doing", "done"],
                    "description": "New status for update_status.",
                },
                "supervision_target_name": {"type": "string", "description": "Supervision target display name."},
                "supervision_channel": {"type": "string", "description": "Channel for supervision reminders."},
                "remind_schedule": {"type": "string", "description": "Human-readable reminder schedule."},
            },
            "required": ["action", "title"],
        },
        category="tasks",
        display_name="Manage Tasks",
        icon="\u270f\ufe0f",
        governance="sensitive",
        adapter="request",
    )
)
async def manage_tasks(request: ToolExecutionRequest) -> str:
    from app.services.agent_tools import _manage_tasks

    return await _manage_tasks(
        request.context.agent_id,
        request.context.user_id,
        Path(request.context.workspace),
        request.arguments,
    )
