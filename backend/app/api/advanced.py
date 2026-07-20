"""Agent collaboration and handover API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._plan_gate import enforce_plan_gate, get_plan_mode_gate, stamp_plan_gate_decision
from app.core.execution_context import ExecutionPrincipal
from app.core.permissions import (
    authorize_session_action,
    check_agent_access,
    effective_agent_owner_id,
    require_agent_owner_or_admin,
)
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.agent_ownership_service import transfer_agent_owner
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
    confirmed_plan_session_id: str | None = None


class InterAgentMessage(BaseModel):
    to_agent_id: uuid.UUID
    message: str
    msg_type: str = "notify"  # notify | consult
    root_session_id: uuid.UUID | None = None


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
    source_agent, _access_level = await check_agent_access(db, current_user, agent_id)
    # Plan Mode early intercept (§9.3): delegation hands execution to another agent
    # to run asynchronously, so it needs a confirmed plan (or cutover exemption).
    action_artifact = {
        "to_agent_id": str(data.to_agent_id),
        "task_title": data.task_title,
        "task_description": data.task_description,
    }
    evidence_id = f"delegation:{agent_id}:{data.to_agent_id}:{uuid.uuid4()}"
    plan_decision = await enforce_plan_gate(
        db,
        agent_id=agent_id,
        requester_user_id=current_user.id,
        session_id=data.confirmed_plan_session_id,
        action_kind="start_delegation",
        target_ref=f"agent:{data.to_agent_id}:delegation",
        gate=get_plan_mode_gate(),
        confirmed_plan_id=data.confirmed_plan_id,
        confirmed_plan_version=data.confirmed_plan_version,
        confirmed_plan_hash=data.confirmed_plan_hash,
        action_artifact=action_artifact,
        evidence_id=evidence_id,
    )
    plan_binding = stamp_plan_gate_decision(
        {},
        decision=plan_decision,
        confirmed_plan_id=data.confirmed_plan_id,
        confirmed_plan_version=data.confirmed_plan_version,
        confirmed_plan_hash=data.confirmed_plan_hash,
        requester_user_id=current_user.id,
        session_id=data.confirmed_plan_session_id,
        evidence_id=evidence_id,
    )
    plan_evidence = plan_binding.get("plan_authorization")
    if plan_evidence:
        # Delegation launches work in a separate durable runtime. Commit the
        # consumed lease before crossing that boundary so a later API-session
        # rollback can never leave an unauthorized child run alive.
        await db.commit()
    try:
        result = await collaboration_service.delegate_task(
            db,
            agent_id,
            data.to_agent_id,
            data.task_title,
            data.task_description,
            principal=ExecutionPrincipal(
                tenant_id=source_agent.tenant_id,
                source_agent_id=source_agent.id,
                requester_user_id=current_user.id,
                root_session_id=data.confirmed_plan_session_id,
                origin="rest",
            ),
            confirmed_plan_id=plan_binding.get("plan_id"),
            confirmed_plan_version=plan_binding.get("plan_version"),
            confirmed_plan_hash=plan_binding.get("plan_hash"),
            confirmed_plan_session_id=data.confirmed_plan_session_id,
            plan_authorization=plan_evidence,
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
    source_agent, _access_level = await check_agent_access(db, current_user, agent_id)
    root_session_id = str(data.root_session_id) if data.root_session_id else None
    if data.root_session_id is not None:
        await authorize_session_action(
            db,
            current_user,
            agent_id=agent_id,
            session_id=data.root_session_id,
            action="a2a_message",
            require_writable=True,
        )
    try:
        return await collaboration_service.send_message_between_agents(
            db,
            agent_id,
            data.to_agent_id,
            data.message,
            data.msg_type,
            principal=ExecutionPrincipal(
                tenant_id=source_agent.tenant_id,
                source_agent_id=source_agent.id,
                requester_user_id=current_user.id,
                root_session_id=root_session_id,
                origin="rest",
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ─── Agent Handover ─────────────────────────────────────


class HandoverRequest(BaseModel):
    new_owner_id: uuid.UUID = Field(validation_alias=AliasChoices("new_owner_id", "new_creator_id"))
    expected_owner_id: uuid.UUID | None = None
    reason: str = Field(default="Manual ownership transfer", min_length=3, max_length=500)
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


@router.get("/agents/{agent_id}/handover-candidates")
async def list_handover_candidates(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List eligible users who can receive ownership of this digital employee."""
    agent = await require_agent_owner_or_admin(db, current_user, agent_id)

    result = await db.execute(
        select(User)
        .where(
            User.tenant_id == agent.tenant_id,
            User.is_active,
            User.id != effective_agent_owner_id(agent),
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
    """Transfer current ownership without rewriting creator/sponsor provenance."""
    receipt = await transfer_agent_owner(
        db,
        agent_id=agent_id,
        new_owner_id=data.new_owner_id,
        actor=current_user,
        reason=data.reason,
        expected_owner_id=data.expected_owner_id,
        request_id=data.request_id,
    )
    return {
        "status": receipt.status,
        "agent_id": str(receipt.agent_id),
        "agent_name": receipt.agent_name,
        "old_owner_id": str(receipt.old_owner_id) if receipt.old_owner_id else None,
        "new_owner_id": str(receipt.new_owner_id),
        "new_owner": receipt.new_owner_name,
        "request_id": receipt.request_id,
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
