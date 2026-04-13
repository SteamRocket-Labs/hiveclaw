"""Schedule API — CRUD for agent cron jobs."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator, is_agent_expired
from app.core.security import get_current_user
from app.database import get_db
from app.models.trigger import AgentTrigger
from app.models.user import User
from app.services.schedule_compat import (
    build_schedule_trigger_config,
    compute_next_run,
    is_schedule_compat_trigger,
    mark_schedule_manual_pending,
    migrate_legacy_schedules,
    schedule_response_payload,
)

router = APIRouter(prefix="/agents/{agent_id}/schedules", tags=["schedules"])


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    instruction: str = Field(default='', max_length=5000)
    cron_expr: str = Field(min_length=1, max_length=100)
    is_enabled: bool = True
    delivery_target_json: dict | None = None


class ScheduleUpdate(BaseModel):
    name: str | None = None
    instruction: str | None = None
    cron_expr: str | None = None
    is_enabled: bool | None = None
    delivery_target_json: dict | None = None


class ScheduleOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    instruction: str
    cron_expr: str
    is_enabled: bool
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int
    created_by: uuid.UUID | None = None
    creator_username: str | None = None
    delivery_target_json: dict | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


async def _load_schedule_trigger(db: AsyncSession, agent_id: uuid.UUID, schedule_id: uuid.UUID) -> AgentTrigger | None:
    result = await db.execute(
        select(AgentTrigger).where(
            AgentTrigger.id == schedule_id,
            AgentTrigger.agent_id == agent_id,
            AgentTrigger.type == "cron",
        )
    )
    trigger = result.scalar_one_or_none()
    if trigger and is_schedule_compat_trigger(trigger):
        return trigger
    return None


@router.get("/", response_model=list[ScheduleOut])
async def list_schedules(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all schedules for an agent."""
    await check_agent_access(db, current_user, agent_id)
    migrated = await migrate_legacy_schedules(db, agent_id)
    if migrated:
        await db.flush()
    result = await db.execute(
        select(AgentTrigger)
        .where(AgentTrigger.agent_id == agent_id, AgentTrigger.type == "cron")
        .order_by(AgentTrigger.created_at.desc())
    )
    schedule_triggers = [trigger for trigger in result.scalars().all() if is_schedule_compat_trigger(trigger)]
    payloads = [schedule_response_payload(trigger) for trigger in schedule_triggers]
    # Batch-load creator usernames
    creator_ids = {payload["created_by"] for payload in payloads if payload["created_by"]}
    creator_map = {}
    if creator_ids:
        users_result = await db.execute(select(User).where(User.id.in_(creator_ids)))
        creator_map = {u.id: u.username for u in users_result.scalars().all()}
    return [
        ScheduleOut.model_validate(
            {
                **payload,
                "creator_username": creator_map.get(payload["created_by"]),
            }
        )
        for payload in payloads
    ]


@router.post("/", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    agent_id: uuid.UUID,
    data: ScheduleCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new schedule for an agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can manage schedules")
    await migrate_legacy_schedules(db, agent_id)

    # Validate cron expression
    if not compute_next_run(data.cron_expr):
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {data.cron_expr}")

    trigger = AgentTrigger(
        agent_id=agent_id,
        name=data.name,
        type="cron",
        config=build_schedule_trigger_config(
            instruction=data.instruction,
            cron_expr=data.cron_expr,
            delivery_target_json=data.delivery_target_json,
            created_by=current_user.id,
        ),
        reason=data.instruction,
        is_enabled=data.is_enabled,
    )
    db.add(trigger)
    await db.flush()
    return ScheduleOut.model_validate(schedule_response_payload(trigger))


@router.patch("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    data: ScheduleUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a schedule."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can manage schedules")
    await migrate_legacy_schedules(db, agent_id)

    trigger = await _load_schedule_trigger(db, agent_id, schedule_id)
    if not trigger:
        raise HTTPException(status_code=404, detail="Schedule not found")

    updates = data.model_dump(exclude_unset=True)
    new_name = updates.get("name", trigger.name)
    new_instruction = updates.get("instruction", trigger.config.get("instruction") or trigger.reason or "")
    new_expr = updates.get("cron_expr", trigger.config.get("expr") or "")
    new_enabled = updates.get("is_enabled", trigger.is_enabled)
    delivery_target_json = (
        updates["delivery_target_json"]
        if "delivery_target_json" in updates
        else trigger.config.get("delivery_target_json")
    )

    if not compute_next_run(new_expr):
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {new_expr}")

    trigger.name = new_name
    trigger.reason = new_instruction
    trigger.is_enabled = new_enabled
    trigger.config = build_schedule_trigger_config(
        instruction=new_instruction,
        cron_expr=new_expr,
        delivery_target_json=delivery_target_json,
        created_by=trigger.config.get("created_by") or current_user.id,
        existing_config=trigger.config,
    )

    await db.flush()
    return ScheduleOut.model_validate(schedule_response_payload(trigger))


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a schedule."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can manage schedules")
    await migrate_legacy_schedules(db, agent_id)

    trigger = await _load_schedule_trigger(db, agent_id, schedule_id)
    if not trigger:
        raise HTTPException(status_code=404, detail="Schedule not found")

    await db.delete(trigger)
    await db.flush()


@router.post("/{schedule_id}/run")
async def trigger_schedule(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger a schedule execution."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired and cannot be triggered.")
    await migrate_legacy_schedules(db, agent_id)

    trigger = await _load_schedule_trigger(db, agent_id, schedule_id)
    if not trigger:
        raise HTTPException(status_code=404, detail="Schedule not found")

    trigger.config = mark_schedule_manual_pending(trigger.config)
    await db.flush()

    return {"status": "triggered", "schedule_id": str(schedule_id)}


@router.get("/{schedule_id}/history")
async def get_schedule_history(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get execution history for a schedule from activity logs."""
    await check_agent_access(db, current_user, agent_id)
    await migrate_legacy_schedules(db, agent_id)
    from app.models.activity_log import AgentActivityLog
    result = await db.execute(
        select(AgentActivityLog)
        .where(
            AgentActivityLog.agent_id == agent_id,
            AgentActivityLog.action_type == "schedule_run",
        )
        .order_by(AgentActivityLog.created_at.desc())
    )
    logs = result.scalars().all()
    # Filter by schedule_id in detail_json
    history = []
    for log in logs:
        detail = log.detail_json or {}
        if detail.get("schedule_id") == str(schedule_id):
            history.append({
                "id": str(log.id),
                "created_at": log.created_at.isoformat() if log.created_at else None,
                "summary": log.summary,
                "instruction": detail.get("instruction", ""),
                "reply": detail.get("reply", ""),
            })
        if len(history) >= 20:
            break
    return history
