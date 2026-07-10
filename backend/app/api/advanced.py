"""Agent collaboration and handover API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._plan_gate import enforce_plan_gate, get_plan_mode_gate
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.collaboration import collaboration_service

router = APIRouter(tags=["advanced"])


# ─── Collaboration ──────────────────────────────────────


class DelegateRequest(BaseModel):
    to_agent_id: uuid.UUID
    task_title: str
    task_description: str = ""
    # Plan Mode (§9.3): a confirmed plan authorising this async delegation.
    confirmed_plan_id: str | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None


class InterAgentMessage(BaseModel):
    to_agent_id: uuid.UUID
    message: str
    msg_type: str = "notify"  # notify | consult


@router.get("/agents/{agent_id}/collaborators")
async def list_collaborators(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List agents that can collaborate with this agent."""
    await check_agent_access(db, current_user, agent_id)
    return await collaboration_service.list_collaborators(db, agent_id)


@router.post("/agents/{agent_id}/collaborate/delegate")
async def delegate_task(
    agent_id: uuid.UUID,
    data: DelegateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delegate a task from one agent to another."""
    await check_agent_access(db, current_user, agent_id)
    # Plan Mode early intercept (§9.3): delegation hands execution to another agent
    # to run asynchronously, so it needs a confirmed plan (or cutover exemption).
    await enforce_plan_gate(
        db,
        agent_id=agent_id,
        action_kind="start_delegation",
        gate=get_plan_mode_gate(),
        confirmed_plan_id=data.confirmed_plan_id,
        confirmed_plan_version=data.confirmed_plan_version,
        confirmed_plan_hash=data.confirmed_plan_hash,
    )
    try:
        result = await collaboration_service.delegate_task(
            db,
            agent_id,
            data.to_agent_id,
            data.task_title,
            data.task_description,
            confirmed_plan_id=data.confirmed_plan_id,
            confirmed_plan_version=data.confirmed_plan_version,
            confirmed_plan_hash=data.confirmed_plan_hash,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/agents/{agent_id}/collaborate/message")
async def send_inter_agent_message(
    agent_id: uuid.UUID,
    data: InterAgentMessage,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a message between agents."""
    await check_agent_access(db, current_user, agent_id)
    return await collaboration_service.send_message_between_agents(
        db, agent_id, data.to_agent_id, data.message, data.msg_type
    )


# ─── Agent Handover ─────────────────────────────────────


class HandoverRequest(BaseModel):
    new_creator_id: uuid.UUID


@router.get("/agents/{agent_id}/handover-candidates")
async def list_handover_candidates(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List eligible users who can receive ownership of this digital employee."""
    from app.core.permissions import is_agent_creator

    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can view handover candidates")

    result = await db.execute(
        select(User)
        .where(
            User.tenant_id == agent.tenant_id,
            User.is_active,
            User.id != agent.creator_id,
        )
        .order_by(User.display_name.asc(), User.username.asc())
    )
    users = result.scalars().all()
    return [
        {
            "id": str(user.id),
            "display_name": user.display_name,
            "email": user.email,
            "role": user.role,
        }
        for user in users
    ]


@router.post("/agents/{agent_id}/handover")
async def handover_agent(
    agent_id: uuid.UUID,
    data: HandoverRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Transfer ownership of a digital employee to another user."""
    from app.core.permissions import is_agent_creator
    from app.models.audit import AuditLog

    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can handover agent")

    # Verify new creator exists
    new_creator_result = await db.execute(select(User).where(User.id == data.new_creator_id))
    new_creator = new_creator_result.scalar_one_or_none()
    if not new_creator:
        raise HTTPException(status_code=404, detail="Target user not found")
    if not new_creator.is_active:
        raise HTTPException(status_code=400, detail="Target user is inactive")
    if str(new_creator.tenant_id) != str(agent.tenant_id):
        raise HTTPException(status_code=400, detail="Target user must belong to the same company")

    old_creator_id = agent.creator_id
    agent.creator_id = data.new_creator_id

    db.add(
        AuditLog(
            user_id=current_user.id,
            agent_id=agent_id,
            tenant_id=agent.tenant_id,
            action="agent:handover",
            details={
                "from_creator": str(old_creator_id),
                "to_creator": str(data.new_creator_id),
            },
        )
    )
    await db.flush()

    return {
        "status": "transferred",
        "agent_name": agent.name,
        "new_creator": new_creator.display_name,
    }


# ─── Observability ──────────────────────────────────────


@router.get("/agents/{agent_id}/metrics")
async def get_agent_metrics(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get observability metrics for an agent."""
    from sqlalchemy import func
    from app.models.task import Task
    from app.models.audit import AuditLog, ApprovalRequest
    from app.services.enterprise_approval_visibility import enterprise_visible_approval_filter

    agent, _access = await check_agent_access(db, current_user, agent_id)

    # Task stats
    total_tasks = await db.execute(select(func.count(Task.id)).where(Task.agent_id == agent_id))
    done_tasks = await db.execute(select(func.count(Task.id)).where(Task.agent_id == agent_id, Task.status == "done"))
    pending_tasks = await db.execute(
        select(func.count(Task.id)).where(Task.agent_id == agent_id, Task.status == "pending")
    )

    # Approval stats
    total_approvals = await db.execute(
        select(func.count(ApprovalRequest.id)).where(
            ApprovalRequest.agent_id == agent_id,
            enterprise_visible_approval_filter(ApprovalRequest),
        )
    )
    pending_approvals = await db.execute(
        select(func.count(ApprovalRequest.id)).where(
            ApprovalRequest.agent_id == agent_id,
            ApprovalRequest.status == "pending",
            enterprise_visible_approval_filter(ApprovalRequest),
        )
    )

    # Recent activity count (last 24h)
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_actions = await db.execute(
        select(func.count(AuditLog.id)).where(AuditLog.agent_id == agent_id, AuditLog.created_at >= cutoff)
    )

    # Container status
    from app.services.agent_manager import agent_manager

    container_status = agent_manager.get_container_status(agent)

    # Extract scalar values (each result can only be consumed once)
    _total_tasks = total_tasks.scalar() or 0
    _done_tasks = done_tasks.scalar() or 0
    _pending_tasks = pending_tasks.scalar() or 0
    _total_approvals = total_approvals.scalar() or 0
    _pending_approvals = pending_approvals.scalar() or 0
    _recent_actions = recent_actions.scalar() or 0

    return {
        "agent_id": str(agent_id),
        "agent_name": agent.name,
        "status": agent.status,
        "container": container_status,
        "tokens": {
            "used_today": agent.tokens_used_today,
            "used_month": agent.tokens_used_month,
            "used_total": agent.tokens_used_total,
        },
        "tasks": {
            "total": _total_tasks,
            "done": _done_tasks,
            "pending": _pending_tasks,
            "completion_rate": round(_done_tasks / max(_total_tasks, 1) * 100, 1),
        },
        "approvals": {
            "total": _total_approvals,
            "pending": _pending_approvals,
        },
        "activity": {
            "actions_last_24h": _recent_actions,
        },
    }
