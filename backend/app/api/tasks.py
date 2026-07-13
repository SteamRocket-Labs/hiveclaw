"""Task management API routes."""

import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._plan_gate import enforce_plan_gate, get_plan_mode_gate, stamp_plan_gate_decision
from app.core.permissions import check_agent_access
from app.core.resource_authority import authorize_resource_action, filter_authorized_resources
from app.core.security import get_current_user
from app.database import get_db
from app.models.task import Task, TaskLog
from app.models.runtime_task import RuntimeTask
from app.models.chat_session import ChatSession
from app.models.user import User
from app.schemas.schemas import BusinessTaskDetailOut, TaskCreate, TaskLogCreate, TaskLogOut, TaskOut, TaskUpdate
from app.services.business_task_runtime import (
    BusinessTaskInvariantError,
    apply_business_task_cancellation,
    business_task_request_key,
    business_task_runtime_root_key,
    project_business_task,
    reconcile_business_task,
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


class TaskCancelIn(BaseModel):
    reason: str = Field(default="", max_length=2_000)


class TaskReconcileIn(BaseModel):
    decision: str
    reason: str = Field(min_length=1, max_length=4_000)


router = APIRouter(prefix="/agents/{agent_id}/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)


async def _task_delivery_context(
    db: AsyncSession,
    *,
    session_id: str | None,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    requester_user_id: uuid.UUID,
) -> tuple[uuid.UUID | None, dict | None]:
    if not session_id:
        return None, None
    try:
        session_uuid = uuid.UUID(str(session_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="confirmed plan session is invalid") from exc
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_uuid,
                ChatSession.tenant_id == tenant_id,
                ChatSession.agent_id == agent_id,
                ChatSession.user_id == requester_user_id,
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=403, detail="confirmed plan session is outside the requester authority")
    target = dict(session.delivery_target_json or {})
    if not target or str(target.get("channel") or "").strip().lower() == "web":
        target = None
    return session.id, target


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
    runtime_task = (
        await db.get(RuntimeTask, task.active_runtime_task_id) if task.active_runtime_task_id is not None else None
    )
    _stamp_task_runtime_projection(out, task=task, runtime_task=runtime_task)
    return out


def _stamp_task_runtime_projection(
    out: TaskOut,
    *,
    task: Task,
    runtime_task: RuntimeTask | None,
) -> TaskOut:
    projection = project_business_task(task=task, runtime_task=runtime_task)
    out.runtime_status = projection["runtime_status"]
    out.runtime_phase = projection["runtime_phase"]
    out.runtime_summary = projection["runtime_summary"]
    out.runtime_request_id = projection["runtime_request_id"]
    out.reflection_session_id = projection["reflection_session_id"]
    out.recovery_state = projection["recovery_state"]
    out.recovery_message = projection["recovery_message"]
    out.actions = projection["actions"]
    out.dependencies = projection["dependencies"]
    out.stages = projection["stages"]
    return out


async def _business_task_detail(task: Task, db: AsyncSession) -> BusinessTaskDetailOut:
    logs = list(
        (await db.execute(select(TaskLog).where(TaskLog.task_id == task.id).order_by(TaskLog.created_at.asc())))
        .scalars()
        .all()
    )
    return BusinessTaskDetailOut(
        task=await _enrich_task_out(task, db),
        logs=[TaskLogOut.model_validate(item) for item in logs],
    )


def _stamp_task_authority(out: TaskOut, decision) -> TaskOut:
    out.authority_source = decision.authority_source
    out.operator_view = decision.operator_view
    return out


async def _authorize_task(
    db: AsyncSession,
    current_user: User,
    *,
    agent_id: uuid.UUID,
    task: Task,
    action: str,
    agent_access,
    operator_view: bool,
    operator_reason: str | None,
):
    return await authorize_resource_action(
        db,
        current_user,
        agent_id=agent_id,
        resource_kind="task",
        resource_id=task.id,
        action=action,
        owner_user_id=task.created_by,
        root_session_id=task.root_session_id,
        authority_state=task.authority_state,
        allow_manager_override=operator_view,
        manager_override_reason=operator_reason,
        agent_access=agent_access,
    )


async def _load_authorized_task(
    db: AsyncSession,
    current_user: User,
    *,
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    action: str,
    operator_view: bool,
    operator_reason: str | None,
    for_update: bool = False,
) -> tuple[Task, object]:
    agent_access = await check_agent_access(db, current_user, agent_id)
    statement = select(Task).where(Task.id == task_id, Task.agent_id == agent_id)
    if for_update:
        statement = statement.with_for_update()
    task = (await db.execute(statement)).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    decision = await _authorize_task(
        db,
        current_user,
        agent_id=agent_id,
        task=task,
        action=action,
        agent_access=agent_access,
        operator_view=operator_view,
        operator_reason=operator_reason,
    )
    return task, decision


@router.get("/", response_model=list[TaskOut])
async def list_tasks(
    agent_id: uuid.UUID,
    status_filter: str | None = None,
    type_filter: str | None = None,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List tasks for an agent."""
    agent_access = await check_agent_access(db, current_user, agent_id)
    query = select(Task).where(Task.agent_id == agent_id)
    if status_filter:
        query = query.where(Task.status == status_filter)
    if type_filter:
        query = query.where(Task.type == type_filter)
    query = query.order_by(Task.created_at.desc())
    result = await db.execute(query)
    tasks_list = result.scalars().all()
    authorized = await filter_authorized_resources(
        db,
        current_user,
        agent_id=agent_id,
        resource_kind="task",
        action="read",
        resources=tasks_list,
        owner_user_id_of=lambda task: task.created_by,
        operator_view=operator_view,
        operator_reason=operator_reason,
        agent_access=agent_access,
    )
    # Batch-load creator usernames
    creator_ids = {task.created_by for task, _decision in authorized if task.created_by}
    creator_map = {}
    if creator_ids:
        users_result = await db.execute(select(User).where(User.id.in_(creator_ids)))
        creator_map = {u.id: u.username for u in users_result.scalars().all()}
    runtime_ids = {task.active_runtime_task_id for task, _decision in authorized if task.active_runtime_task_id}
    runtime_map: dict[uuid.UUID, RuntimeTask] = {}
    if runtime_ids:
        runtime_result = await db.execute(select(RuntimeTask).where(RuntimeTask.id.in_(runtime_ids)))
        runtime_map = {runtime.id: runtime for runtime in runtime_result.scalars().all()}
    out_list = []
    for t, decision in authorized:
        t_out = TaskOut.model_validate(t)
        t_out.creator_username = creator_map.get(t.created_by)
        _stamp_task_authority(t_out, decision)
        _stamp_task_runtime_projection(t_out, task=t, runtime_task=runtime_map.get(t.active_runtime_task_id))
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
        authority_state="owned",
        request_id=data.request_id,
        request_hash=request_hash,
        plan_id=uuid.UUID(data.confirmed_plan_id) if data.confirmed_plan_id else None,
        plan_version=data.confirmed_plan_version,
        plan_hash=data.confirmed_plan_hash,
        plan_authorization=plan_evidence,
    )
    root_session_id, delivery_target = await _task_delivery_context(
        db,
        session_id=data.confirmed_plan_session_id,
        tenant_id=agent.tenant_id,
        agent_id=agent_id,
        requester_user_id=current_user.id,
    )
    task.root_session_id = root_session_id
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
            root_session_id=root_session_id,
            delivery_target=delivery_target,
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


@router.get("/{task_id}", response_model=BusinessTaskDetailOut)
async def get_task_detail(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task, decision = await _load_authorized_task(
        db,
        current_user,
        agent_id=agent_id,
        task_id=task_id,
        action="read",
        operator_view=operator_view,
        operator_reason=operator_reason,
    )
    detail = await _business_task_detail(task, db)
    _stamp_task_authority(detail.task, decision)
    return detail


@router.post("/{task_id}/cancel", response_model=BusinessTaskDetailOut)
async def cancel_business_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskCancelIn,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task, decision = await _load_authorized_task(
        db,
        current_user,
        agent_id=agent_id,
        task_id=task_id,
        action="execute",
        operator_view=operator_view,
        operator_reason=operator_reason,
        for_update=True,
    )
    runtime_task = (
        await db.get(RuntimeTask, task.active_runtime_task_id, with_for_update=True)
        if task.active_runtime_task_id is not None
        else None
    )
    if runtime_task is None:
        raise HTTPException(status_code=409, detail="Business task has no active runtime intent to cancel")
    runtime_metadata = dict(runtime_task.metadata_json or {})
    if task.status in {"cancelled", "needs_reconciliation"} and runtime_metadata.get("cancelled_by_user_id"):
        detail = await _business_task_detail(task, db)
        _stamp_task_authority(detail.task, decision)
        return detail
    try:
        outcome = apply_business_task_cancellation(
            db=db,
            task=task,
            runtime_task=runtime_task,
            cancelled_by_user_id=current_user.id,
            reason=data.reason,
        )
    except BusinessTaskInvariantError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from app.core.policy import write_audit_event

    await write_audit_event(
        db,
        event_type="business_task.cancelled",
        severity="warning" if outcome.status.value == "needs_reconciliation" else "info",
        actor_type="user",
        actor_id=current_user.id,
        tenant_id=task.tenant_id,
        action="cancel_business_task",
        resource_type="task",
        resource_id=task.id,
        details={
            "runtime_task_id": str(runtime_task.id),
            "outcome": outcome.status.value,
            "retryable": outcome.retryable,
        },
    )
    await db.commit()
    try:
        from app.services.runtime_control_bus import publish_business_task_cancel

        await publish_business_task_cancel(task_id=task.id, runtime_task_id=runtime_task.id)
    except Exception as exc:  # noqa: BLE001 - DB status and claim fence remain authoritative.
        logger.warning("business task cancel wakeup failed for %s: %s", runtime_task.id, exc)
    detail = await _business_task_detail(task, db)
    _stamp_task_authority(detail.task, decision)
    return detail


@router.post("/{task_id}/reconcile", response_model=BusinessTaskDetailOut)
async def reconcile_business_task_state(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskReconcileIn,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task, decision = await _load_authorized_task(
        db,
        current_user,
        agent_id=agent_id,
        task_id=task_id,
        action="execute",
        operator_view=operator_view,
        operator_reason=operator_reason,
        for_update=True,
    )
    runtime_task = (
        await db.get(RuntimeTask, task.active_runtime_task_id, with_for_update=True)
        if task.active_runtime_task_id is not None
        else None
    )
    if runtime_task is None:
        raise HTTPException(status_code=409, detail="Business task runtime evidence is missing")
    try:
        reconcile_business_task(
            db=db,
            task=task,
            runtime_task=runtime_task,
            resolved_by_user_id=current_user.id,
            decision=data.decision,
            reason=data.reason,
        )
    except (BusinessTaskInvariantError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from app.core.policy import write_audit_event

    await write_audit_event(
        db,
        event_type="business_task.reconciled",
        severity="warning",
        actor_type="user",
        actor_id=current_user.id,
        tenant_id=task.tenant_id,
        action="reconcile_business_task",
        resource_type="task",
        resource_id=task.id,
        details={
            "runtime_task_id": str(runtime_task.id),
            "decision": data.decision,
            "automatic_retry_allowed": data.decision == "retry_safe",
        },
    )
    await db.commit()
    detail = await _business_task_detail(task, db)
    _stamp_task_authority(detail.task, decision)
    return detail


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskUpdate,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a task."""
    agent_access = await check_agent_access(db, current_user, agent_id)
    result = await db.execute(select(Task).where(Task.id == task_id, Task.agent_id == agent_id).with_for_update())
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    decision = await _authorize_task(
        db,
        current_user,
        agent_id=agent_id,
        task=task,
        action="write",
        agent_access=agent_access,
        operator_view=operator_view,
        operator_reason=operator_reason,
    )

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    await db.flush()
    return _stamp_task_authority(await _enrich_task_out(task, db), decision)


@router.get("/{task_id}/logs", response_model=list[TaskLogOut])
async def get_task_logs(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get progress logs for a task."""
    agent_access = await check_agent_access(db, current_user, agent_id)
    task = (await db.execute(select(Task).where(Task.id == task_id, Task.agent_id == agent_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await _authorize_task(
        db,
        current_user,
        agent_id=agent_id,
        task=task,
        action="read",
        agent_access=agent_access,
        operator_view=operator_view,
        operator_reason=operator_reason,
    )
    result = await db.execute(select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.created_at.asc()))
    return [TaskLogOut.model_validate(log_item) for log_item in result.scalars().all()]


@router.post("/{task_id}/logs", response_model=TaskLogOut, status_code=status.HTTP_201_CREATED)
async def add_task_log(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskLogCreate,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a progress log entry to a task."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    task = (await db.execute(select(Task).where(Task.id == task_id, Task.agent_id == agent_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await _authorize_task(
        db,
        current_user,
        agent_id=agent_id,
        task=task,
        action="write",
        agent_access=(agent, access_level),
        operator_view=operator_view,
        operator_reason=operator_reason,
    )
    log = TaskLog(tenant_id=agent.tenant_id, task_id=task_id, content=data.content)
    db.add(log)
    await db.flush()
    return TaskLogOut.model_validate(log)


async def _trigger_task_run(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskTriggerIn,
    operator_view: bool,
    operator_reason: str | None,
    current_user: User,
    db: AsyncSession,
    *,
    retry_only: bool,
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
    await _authorize_task(
        db,
        current_user,
        agent_id=agent_id,
        task=task,
        action="execute",
        agent_access=(agent, _access),
        operator_view=operator_view,
        operator_reason=operator_reason,
    )

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
    if retry_only:
        active_runtime = (
            await db.get(RuntimeTask, task.active_runtime_task_id) if task.active_runtime_task_id is not None else None
        )
        projection = project_business_task(task=task, runtime_task=active_runtime)
        if not projection["actions"]["can_retry"]:
            raise HTTPException(
                status_code=409,
                detail=projection["recovery_message"] or "Business task is not safe to retry",
            )
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
    task.plan_id = uuid.UUID(trigger_in.confirmed_plan_id) if trigger_in.confirmed_plan_id else None
    task.plan_version = trigger_in.confirmed_plan_version
    task.plan_hash = trigger_in.confirmed_plan_hash

    root_session_id, delivery_target = await _task_delivery_context(
        db,
        session_id=trigger_in.confirmed_plan_session_id,
        tenant_id=agent.tenant_id,
        agent_id=agent_id,
        requester_user_id=current_user.id,
    )

    try:
        runtime_task = await stage_business_task_runtime(
            db=db,
            task=task,
            requester_user_id=current_user.id,
            agent_name=getattr(agent, "name", None),
            request_id=trigger_in.request_id,
            request_hash=request_hash,
            root_session_id=root_session_id,
            delivery_target=delivery_target,
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


@router.post("/{task_id}/trigger")
async def trigger_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskTriggerIn,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _trigger_task_run(
        agent_id,
        task_id,
        data,
        operator_view,
        operator_reason,
        current_user,
        db,
        retry_only=False,
    )


@router.post("/{task_id}/retry")
async def retry_business_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskTriggerIn,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _trigger_task_run(
        agent_id,
        task_id,
        data,
        operator_view,
        operator_reason,
        current_user,
        db,
        retry_only=True,
    )
