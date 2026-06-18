"""Task management API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._plan_gate import enforce_plan_gate, get_plan_mode_gate
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.task import Task, TaskLog
from app.models.user import User
from app.schemas.schemas import TaskCreate, TaskLogCreate, TaskLogOut, TaskOut, TaskUpdate


class TaskTriggerIn(BaseModel):
    # Plan Mode (§9.3): a confirmed plan authorising this auto-executing task run.
    confirmed_plan_id: str | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None


router = APIRouter(prefix="/agents/{agent_id}/tasks", tags=["tasks"])


async def _enrich_task_out(task: Task, db: AsyncSession) -> TaskOut:
    """Convert Task to TaskOut with creator_username populated."""
    out = TaskOut.model_validate(task)
    if task.created_by:
        user_result = await db.execute(select(User).where(User.id == task.created_by))
        user = user_result.scalar_one_or_none()
        if user:
            out.creator_username = user.username
    return out


@router.get("/", response_model=list[TaskOut])
async def list_tasks(
    agent_id: uuid.UUID,
    status_filter: str | None = None,
    type_filter: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List tasks for an agent."""
    await check_agent_access(db, current_user, agent_id)
    query = select(Task).where(Task.agent_id == agent_id)
    if status_filter:
        query = query.where(Task.status == status_filter)
    if type_filter:
        query = query.where(Task.type == type_filter)
    query = query.order_by(Task.created_at.desc())
    result = await db.execute(query)
    tasks_list = result.scalars().all()
    # Batch-load creator usernames
    creator_ids = {t.created_by for t in tasks_list if t.created_by}
    creator_map = {}
    if creator_ids:
        users_result = await db.execute(select(User).where(User.id.in_(creator_ids)))
        creator_map = {u.id: u.username for u in users_result.scalars().all()}
    out_list = []
    for t in tasks_list:
        t_out = TaskOut.model_validate(t)
        t_out.creator_username = creator_map.get(t.created_by)
        out_list.append(t_out)
    return out_list


@router.post("/", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    agent_id: uuid.UUID,
    data: TaskCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new task for an agent."""
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    # Plan Mode early intercept (§9.3): a todo task auto-executes in the
    # background (execute_task), so it needs a confirmed plan.
    await enforce_plan_gate(
        db,
        agent_id=agent_id,
        action_kind="start_long_task",
        gate=get_plan_mode_gate(),
        confirmed_plan_id=data.confirmed_plan_id,
        confirmed_plan_version=data.confirmed_plan_version,
        confirmed_plan_hash=data.confirmed_plan_hash,
    )
    task = Task(
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
        title=data.title,
        description=data.description,
        type=data.type,
        priority=data.priority,
        due_date=data.due_date,
        created_by=current_user.id,
        plan_id=uuid.UUID(data.confirmed_plan_id) if data.confirmed_plan_id else None,
        plan_version=data.confirmed_plan_version,
        plan_hash=data.confirmed_plan_hash,
    )
    db.add(task)
    await db.flush()

    task_out = await _enrich_task_out(task, db)

    # Commit so the background executor can see the task in its own session
    await db.commit()

    # Fire background execution
    import asyncio
    from app.services.task_executor import execute_task

    asyncio.create_task(execute_task(task.id, agent_id))

    return task_out


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a task."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(select(Task).where(Task.id == task_id, Task.agent_id == agent_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    await db.flush()
    return await _enrich_task_out(task, db)


@router.get("/{task_id}/logs", response_model=list[TaskLogOut])
async def get_task_logs(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get progress logs for a task."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.created_at.asc()))
    return [TaskLogOut.model_validate(log_item) for log_item in result.scalars().all()]


@router.post("/{task_id}/logs", response_model=TaskLogOut, status_code=status.HTTP_201_CREATED)
async def add_task_log(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskLogCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a progress log entry to a task."""
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    log = TaskLog(tenant_id=agent.tenant_id, task_id=task_id, content=data.content)
    db.add(log)
    await db.flush()
    return TaskLogOut.model_validate(log)


@router.post("/{task_id}/trigger")
async def trigger_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskTriggerIn | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger a task execution (for testing)."""
    from app.core.permissions import is_agent_expired

    agent, _access = await check_agent_access(db, current_user, agent_id)
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired")

    result = await db.execute(select(Task).where(Task.id == task_id, Task.agent_id == agent_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Plan Mode early intercept (§9.3): a manual trigger fires the background
    # execute_task loop, so it needs a confirmed plan.
    trigger_in = data or TaskTriggerIn()
    await enforce_plan_gate(
        db,
        agent_id=agent_id,
        action_kind="start_long_task",
        gate=get_plan_mode_gate(),
        confirmed_plan_id=trigger_in.confirmed_plan_id,
        confirmed_plan_version=trigger_in.confirmed_plan_version,
        confirmed_plan_hash=trigger_in.confirmed_plan_hash,
    )

    import asyncio
    from app.services.task_executor import execute_task

    asyncio.create_task(execute_task(task.id, agent_id))

    return {"status": "triggered", "task_id": str(task_id)}
