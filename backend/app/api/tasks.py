"""Task management API routes."""

import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._plan_gate import enforce_plan_gate, get_plan_mode_gate, stamp_plan_gate_decision
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.task import Task, TaskLog
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.schemas.schemas import TaskCreate, TaskLogCreate, TaskLogOut, TaskOut, TaskUpdate
from app.services.business_task_runtime import (
    BusinessTaskInvariantError,
    business_task_request_key,
    business_task_runtime_root_key,
    stage_business_task_runtime,
)
from app.services.runtime_task_worker import notify_runtime_task_worker


class TaskTriggerIn(BaseModel):
    request_id: str
    # Plan Mode (§9.3): a confirmed plan authorising this auto-executing task run.
    confirmed_plan_id: str | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None
    confirmed_plan_session_id: str | None = None


router = APIRouter(prefix="/agents/{agent_id}/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)


async def _load_matching_task_request(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    request_id: str,
    request_hash: str,
) -> Task | None:
    task = (
        await db.execute(
            select(Task).where(
                Task.tenant_id == tenant_id,
                Task.agent_id == agent_id,
                Task.request_id == request_id,
            )
        )
    ).scalar_one_or_none()
    if task is not None and task.request_hash != request_hash:
        raise HTTPException(status_code=409, detail="request_id was already used for a different task payload")
    return task


async def _load_matching_runtime_request(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    request_id: str,
    request_hash: str,
) -> RuntimeTask | None:
    root_key = business_task_runtime_root_key(task_id=task_id, request_id=request_id)
    runtime_task = (
        await db.execute(
            select(RuntimeTask).where(
                RuntimeTask.root_idempotency_key == root_key,
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.parent_agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()
    if runtime_task is None:
        return None
    metadata = dict(runtime_task.metadata_json or {})
    authority_matches = (
        runtime_task.task_type == "business_task"
        and metadata.get("business_task_id") == str(task_id)
        and metadata.get("requester_user_id") == str(requester_user_id)
        and metadata.get("request_hash") == request_hash
    )
    if not authority_matches:
        raise HTTPException(status_code=409, detail="request_id was already used for a different trigger payload")
    return runtime_task


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
    if agent.tenant_id is None:
        raise HTTPException(status_code=409, detail="Agent tenant is required for task execution")
    # Plan Mode early intercept (§9.3): a todo task auto-executes in the
    # background (execute_task), so it needs a confirmed plan.
    action_artifact = data.model_dump(mode="json")
    request_hash = business_task_request_key(
        tenant_id=agent.tenant_id,
        agent_id=agent_id,
        requester_user_id=current_user.id,
        action="create",
        payload=action_artifact,
    )
    existing = await _load_matching_task_request(
        db,
        tenant_id=agent.tenant_id,
        agent_id=agent_id,
        request_id=data.request_id,
        request_hash=request_hash,
    )
    if existing is not None:
        return await _enrich_task_out(existing, db)
    evidence_id = f"task-create:{uuid.uuid4()}"
    plan_decision = await enforce_plan_gate(
        db,
        agent_id=agent_id,
        requester_user_id=current_user.id,
        session_id=data.confirmed_plan_session_id,
        action_kind="start_long_task",
        target_ref="task:new",
        gate=get_plan_mode_gate(),
        confirmed_plan_id=data.confirmed_plan_id,
        confirmed_plan_version=data.confirmed_plan_version,
        confirmed_plan_hash=data.confirmed_plan_hash,
        action_artifact=action_artifact,
        evidence_id=evidence_id,
    )
    plan_evidence = stamp_plan_gate_decision(
        {},
        decision=plan_decision,
        confirmed_plan_id=data.confirmed_plan_id,
        confirmed_plan_version=data.confirmed_plan_version,
        confirmed_plan_hash=data.confirmed_plan_hash,
        requester_user_id=current_user.id,
        session_id=data.confirmed_plan_session_id,
        evidence_id=evidence_id,
    ).get("plan_authorization")
    task = Task(
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
        title=data.title,
        description=data.description,
        type=data.type,
        priority=data.priority,
        due_date=data.due_date,
        created_by=current_user.id,
        request_id=data.request_id,
        request_hash=request_hash,
        plan_id=uuid.UUID(data.confirmed_plan_id) if data.confirmed_plan_id else None,
        plan_version=data.confirmed_plan_version,
        plan_hash=data.confirmed_plan_hash,
        plan_authorization=plan_evidence,
    )
    try:
        db.add(task)
        await db.flush()
        runtime_task = await stage_business_task_runtime(
            db=db,
            task=task,
            requester_user_id=current_user.id,
            agent_name=getattr(agent, "name", None),
            request_id=data.request_id,
            request_hash=request_hash,
        )
        task_out = await _enrich_task_out(task, db)
        await db.commit()
    except BusinessTaskInvariantError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        await db.rollback()
        existing = await _load_matching_task_request(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent_id,
            request_id=data.request_id,
            request_hash=request_hash,
        )
        if existing is None:
            raise HTTPException(status_code=409, detail="task request conflict could not be recovered") from exc
        return await _enrich_task_out(existing, db)
    try:
        await notify_runtime_task_worker(reason="business_task_created", runtime_task_id=runtime_task.id)
    except Exception as exc:  # polling remains a durable fallback after the committed intent.
        logger.warning("business task wakeup failed for %s: %s", runtime_task.id, exc)

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
    result = await db.execute(select(Task).where(Task.id == task_id, Task.agent_id == agent_id).with_for_update())
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
    task = (await db.execute(select(Task).where(Task.id == task_id, Task.agent_id == agent_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
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
    task = (await db.execute(select(Task).where(Task.id == task_id, Task.agent_id == agent_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    log = TaskLog(tenant_id=agent.tenant_id, task_id=task_id, content=data.content)
    db.add(log)
    await db.flush()
    return TaskLogOut.model_validate(log)


@router.post("/{task_id}/trigger")
async def trigger_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskTriggerIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger a task execution (for testing)."""
    from app.core.permissions import is_agent_expired

    agent, _access = await check_agent_access(db, current_user, agent_id)
    if agent.tenant_id is None:
        raise HTTPException(status_code=409, detail="Agent tenant is required for task execution")
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired")

    result = await db.execute(select(Task).where(Task.id == task_id, Task.agent_id == agent_id).with_for_update())
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Plan Mode early intercept (§9.3): a manual trigger fires the background
    # execute_task loop, so it needs a confirmed plan.
    trigger_in = data
    action_artifact = {
        "task_id": str(task.id),
        "title": getattr(task, "title", ""),
        "description": getattr(task, "description", None),
        "type": getattr(task, "type", "todo"),
        "priority": getattr(task, "priority", "medium"),
        "due_date": task.due_date.isoformat() if getattr(task, "due_date", None) else None,
    }
    request_hash = business_task_request_key(
        tenant_id=agent.tenant_id,
        agent_id=agent_id,
        requester_user_id=current_user.id,
        action="trigger",
        payload={**action_artifact, **trigger_in.model_dump(mode="json")},
    )
    existing_run = await _load_matching_runtime_request(
        db,
        tenant_id=agent.tenant_id,
        agent_id=agent_id,
        task_id=task.id,
        requester_user_id=current_user.id,
        request_id=trigger_in.request_id,
        request_hash=request_hash,
    )
    if existing_run is not None:
        return {"status": "triggered", "task_id": str(task_id), "runtime_task_id": existing_run.id.hex}
    evidence_id = f"task-run:{task.id}:{uuid.uuid4()}"
    plan_decision = await enforce_plan_gate(
        db,
        agent_id=agent_id,
        requester_user_id=current_user.id,
        session_id=trigger_in.confirmed_plan_session_id,
        action_kind="start_long_task",
        target_ref=f"task:{task.id}:run",
        gate=get_plan_mode_gate(),
        confirmed_plan_id=trigger_in.confirmed_plan_id,
        confirmed_plan_version=trigger_in.confirmed_plan_version,
        confirmed_plan_hash=trigger_in.confirmed_plan_hash,
        action_artifact=action_artifact,
        evidence_id=evidence_id,
    )
    task.plan_authorization = stamp_plan_gate_decision(
        {},
        decision=plan_decision,
        confirmed_plan_id=trigger_in.confirmed_plan_id,
        confirmed_plan_version=trigger_in.confirmed_plan_version,
        confirmed_plan_hash=trigger_in.confirmed_plan_hash,
        requester_user_id=current_user.id,
        session_id=trigger_in.confirmed_plan_session_id,
        evidence_id=evidence_id,
    ).get("plan_authorization")

    try:
        runtime_task = await stage_business_task_runtime(
            db=db,
            task=task,
            requester_user_id=current_user.id,
            agent_name=getattr(agent, "name", None),
            request_id=trigger_in.request_id,
            request_hash=request_hash,
        )
        await db.commit()
    except BusinessTaskInvariantError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        await db.rollback()
        existing_run = await _load_matching_runtime_request(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent_id,
            task_id=task.id,
            requester_user_id=current_user.id,
            request_id=trigger_in.request_id,
            request_hash=request_hash,
        )
        if existing_run is None:
            raise HTTPException(status_code=409, detail="task trigger conflict could not be recovered") from exc
        return {
            "status": "triggered",
            "task_id": str(task_id),
            "runtime_task_id": existing_run.id.hex,
        }
    try:
        await notify_runtime_task_worker(reason="business_task_triggered", runtime_task_id=runtime_task.id)
    except Exception as exc:
        logger.warning("business task trigger wakeup failed for %s: %s", runtime_task.id, exc)

    return {"status": "triggered", "task_id": str(task_id), "runtime_task_id": runtime_task.id.hex}
