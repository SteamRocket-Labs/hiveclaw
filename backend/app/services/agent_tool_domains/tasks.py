"""Task management domain — create/update/delete tasks with DB sync.

``_manage_tasks`` is pure CRUD (no auto-execution).  It is called by the REST
layer (``api/tasks.py``) which triggers ``execute_task`` separately after a
human-confirmed plan.  The agent-facing todo board was retired from this path
in F-2 (single-board convergence); agents use ``track_todo`` / ``read_ledger``
instead.
"""

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.task import Task
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.services.business_task_runtime import business_task_request_key

logger = logging.getLogger(__name__)


async def _manage_tasks(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    ws: Path,
    args: dict,
) -> str:
    """Create / update / delete tasks in DB and sync to workspace."""
    from app.models.task import TaskLog
    from app.tools.workspace import _sync_tasks_to_file

    action = args["action"]
    title = args["title"]

    # RLS 阶段2b: tasks + task_logs are USING-only. Pin the GUC to the agent's
    # tenant so SELECT/UPDATE/DELETE survive the non-owner role and stamp
    # tenant_id on new tasks — a NULL would be globally visible.
    tenant_id = await resolve_tenant_for_agent(agent_id)
    if tenant_id is None:
        raise RuntimeError("Agent tenant authority is required for task management")
    async with tenant_scoped_session(tenant_id) as db:
        if action == "create":
            task_type = args.get("task_type", "todo")
            request_id = str(args.get("request_id") or f"internal:{uuid.uuid4()}")
            request_hash = business_task_request_key(
                tenant_id=tenant_id,
                agent_id=agent_id,
                requester_user_id=user_id,
                action="manual_crud_create",
                payload=dict(args),
            )
            task = Task(
                agent_id=agent_id,
                tenant_id=tenant_id,
                title=title,
                description=args.get("description"),
                type=task_type,
                priority=args.get("priority", "medium"),
                created_by=user_id,
                status="pending",
                request_id=request_id,
                request_hash=request_hash,
                plan_id=uuid.UUID(str(args["confirmed_plan_id"])) if args.get("confirmed_plan_id") else None,
                plan_version=args.get("confirmed_plan_version"),
                plan_hash=args.get("confirmed_plan_hash"),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)

            await _sync_tasks_to_file(agent_id, ws)
            return f"✅ Task created: {title} (id={task.id})"

        elif action == "update_status":
            result = await db.execute(select(Task).where(Task.agent_id == agent_id, Task.title.ilike(f"%{title}%")))
            task = result.scalars().first()
            if not task:
                return f"No task found matching '{title}'"
            if task.active_runtime_task_id is not None:
                return "Cannot update: linked execution task is read-only"
            old = task.status
            task.status = args["status"]
            if args["status"] == "done":
                task.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await _sync_tasks_to_file(agent_id, ws)
            return f"✅ Updated '{task.title}' from {old} to {args['status']}"

        elif action == "delete":
            from sqlalchemy import delete as sa_delete

            result = await db.execute(select(Task).where(Task.agent_id == agent_id, Task.title.ilike(f"%{title}%")))
            task = result.scalars().first()
            if not task:
                return f"No task found matching '{title}'"
            if task.active_runtime_task_id is not None:
                return "Cannot delete: linked execution task is read-only"
            task_title = task.title
            await db.execute(sa_delete(TaskLog).where(TaskLog.task_id == task.id))
            await db.delete(task)
            await db.commit()
            await _sync_tasks_to_file(agent_id, ws)
            return f"✅ Task deleted: {task_title}"

        return f"Unknown action: {action}"
