"""Task tools — DB-backed task service helpers (REST-facing only).

F-2 single-board convergence: ``manage_tasks``, ``list_tasks``, and
``get_task`` are **no longer registered as LLM-callable tools**.  The agent
board is now Work Ledger only (``track_todo`` / ``read_ledger``).

The underlying service functions ``_list_tasks_for_agent`` and
``_get_task_for_agent`` are kept here so ``api/tasks.py`` can reuse them
without depending on the tool-decorator machinery.  ``_manage_tasks`` lives in
``services/agent_tool_domains/tasks.py`` and is imported by ``api/tasks.py``
directly.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.task import Task, TaskLog
from app.services.tenant_resolver import resolve_tenant_for_agent


def _task_line(task: Task) -> str:
    due = f", due={task.due_date.isoformat()}" if task.due_date else ""
    return f"- {task.title} [{task.status}/{task.priority}/{task.type}] id={task.id}{due}"


async def _list_tasks_for_agent(agent_id: uuid.UUID, status: str = "", limit: int = 20) -> str:
    """List DB-backed tasks for an agent.  Called by REST; not an LLM tool."""
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20

    # RLS 阶段2b: tasks now bears a USING-only policy. Pin the GUC to the
    # agent's tenant so this read survives the non-owner role (a bare session
    # would fail closed and report "no tasks").
    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
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


async def _get_task_for_agent(agent_id: uuid.UUID, task_id_raw: str = "", title: str = "") -> str:
    """Read one DB-backed task by id or title substring.  Called by REST; not an LLM tool."""
    if not task_id_raw and not title:
        return "Provide `task_id` or `title`."

    # RLS 阶段2b: tasks + task_logs now bear USING-only policies. Scope the
    # read to the agent's tenant so it survives the non-owner role.
    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
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
