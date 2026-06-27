"""Agent-to-Agent collaboration API.

This is the only HTTP surface for governed A2A collaboration. Legacy
Relationship APIs and `relationships.md` materialization are intentionally not
used here; the live collaborator projection is derived from owner/public/group
state in the database.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember
from app.models.user import User
from app.services.a2a_collaboration_policy import build_a2a_collaboration_read_model, resolve_agent_owner_id
from app.services.workspace_sync_dirty import mark_agent_dirty

router = APIRouter(prefix="/agents/{agent_id}/a2a", tags=["a2a"])


class CollaborationGroupCreateIn(BaseModel):
    name: str
    purpose: str = ""
    visibility: str = "group_members"


class CollaborationGroupInviteIn(BaseModel):
    target_agent_id: str
    role: str = "member"
    invitation_reason: str = ""
    capability_scope: dict = Field(default_factory=dict)


class CollaborationGroupMemberUpdateIn(BaseModel):
    reason: str = ""


@router.get("/collaborators")
async def get_a2a_collaborators(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return current A2A collaborators: same-owner, public, and active groups."""

    await check_agent_access(db, current_user, agent_id)
    return await build_a2a_collaboration_read_model(db, agent_id)


@router.post("/groups")
async def create_a2a_group(
    agent_id: uuid.UUID,
    data: CollaborationGroupCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an A2A Collaboration Group owned from this source agent."""

    source_agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manage access required")

    owner_id = resolve_agent_owner_id(source_agent) or current_user.id
    group = AgentCollaborationGroup(
        tenant_id=source_agent.tenant_id,
        name=data.name.strip() or f"{source_agent.name} collaboration group",
        purpose=data.purpose.strip(),
        visibility=data.visibility or "group_members",
        created_by_user_id=current_user.id,
        created_by_agent_id=source_agent.id,
        status="active",
    )
    db.add(group)
    await db.flush()

    db.add(
        AgentCollaborationGroupMember(
            tenant_id=source_agent.tenant_id,
            group_id=group.id,
            agent_id=source_agent.id,
            agent_owner_user_id=owner_id,
            role="owner",
            status="active",
            invited_by_user_id=current_user.id,
            invited_by_agent_id=source_agent.id,
            approved_by_user_id=current_user.id,
            approved_at=datetime.now(timezone.utc),
        )
    )
    await db.flush()
    mark_agent_dirty(agent_id)
    await db.commit()
    return {"status": "ok", "group_id": str(group.id), "group_name": group.name}


@router.post("/groups/{group_id}/members")
async def invite_a2a_group_member(
    agent_id: uuid.UUID,
    group_id: uuid.UUID,
    data: CollaborationGroupInviteIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Invite an agent into a collaboration group.

    Same-owner targets become active immediately. Cross-owner targets enter
    pending_owner_confirmation and require the target owner/admin to approve.
    """

    source_agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manage access required")

    group = await db.get(AgentCollaborationGroup, group_id)
    if not group or group.tenant_id != source_agent.tenant_id or group.status != "active":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="A2A Collaboration Group not found")

    source_membership_result = await db.execute(
        select(AgentCollaborationGroupMember).where(
            AgentCollaborationGroupMember.group_id == group_id,
            AgentCollaborationGroupMember.agent_id == source_agent.id,
            AgentCollaborationGroupMember.status == "active",
        )
    )
    if source_membership_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Source agent is not active in this group")

    try:
        target_agent_id = uuid.UUID(data.target_agent_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid target_agent_id") from exc
    target_result = await db.execute(
        select(Agent).where(
            Agent.id == target_agent_id,
            Agent.id != agent_id,
            Agent.tenant_id == source_agent.tenant_id,
            Agent.deleted_at.is_(None),
        )
    )
    target_agent = target_result.scalar_one_or_none()
    if target_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target agent not found")

    target_owner_id = resolve_agent_owner_id(target_agent)
    if target_owner_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Target agent has no owner")
    source_owner_id = resolve_agent_owner_id(source_agent)
    member_status = "active" if source_owner_id == target_owner_id else "pending_owner_confirmation"
    now = datetime.now(timezone.utc)

    existing_result = await db.execute(
        select(AgentCollaborationGroupMember).where(
            AgentCollaborationGroupMember.group_id == group_id,
            AgentCollaborationGroupMember.agent_id == target_agent.id,
        )
    )
    member = existing_result.scalar_one_or_none()
    if member is None:
        member = AgentCollaborationGroupMember(
            tenant_id=source_agent.tenant_id,
            group_id=group_id,
            agent_id=target_agent.id,
            agent_owner_user_id=target_owner_id,
        )
        db.add(member)
    member.role = data.role or "member"
    member.status = member_status
    member.invited_by_user_id = current_user.id
    member.invited_by_agent_id = source_agent.id
    member.invitation_reason = data.invitation_reason or ""
    member.capability_scope = data.capability_scope or {}
    if member_status == "active":
        member.approved_by_user_id = current_user.id
        member.approved_at = now
        member.rejected_at = None
        member.revoked_at = None
    await db.flush()
    mark_agent_dirty(agent_id)
    mark_agent_dirty(target_agent.id)
    await db.commit()
    return {
        "status": "ok",
        "member_id": str(member.id),
        "member_status": member.status,
        "requires_owner_confirmation": member.status == "pending_owner_confirmation",
    }


def _can_moderate_a2a_member(current_user: User, member: AgentCollaborationGroupMember) -> bool:
    if current_user.role in {"platform_admin", "org_admin"}:
        return True
    return member.agent_owner_user_id == current_user.id


async def _load_group_member_or_404(
    db: AsyncSession,
    *,
    source_agent: Agent,
    group_id: uuid.UUID,
    member_id: uuid.UUID,
) -> AgentCollaborationGroupMember:
    member = await db.get(AgentCollaborationGroupMember, member_id)
    if not member or member.group_id != group_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="A2A group member not found")
    group = await db.get(AgentCollaborationGroup, group_id)
    if not group or group.tenant_id != source_agent.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="A2A Collaboration Group not found")
    return member


@router.post("/groups/{group_id}/members/{member_id}/approve")
async def approve_a2a_group_member(
    agent_id: uuid.UUID,
    group_id: uuid.UUID,
    member_id: uuid.UUID,
    data: CollaborationGroupMemberUpdateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    source_agent, _access_level = await check_agent_access(db, current_user, agent_id)
    member = await _load_group_member_or_404(db, source_agent=source_agent, group_id=group_id, member_id=member_id)
    if not _can_moderate_a2a_member(current_user, member):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Target owner approval required")
    member.status = "active"
    member.approved_by_user_id = current_user.id
    member.approved_at = datetime.now(timezone.utc)
    member.rejected_at = None
    member.revoked_at = None
    member.metadata_json = {**(member.metadata_json or {}), "approval_reason": data.reason}
    await db.flush()
    mark_agent_dirty(agent_id)
    mark_agent_dirty(member.agent_id)
    await db.commit()
    return {"status": "ok", "member_status": member.status}


@router.post("/groups/{group_id}/members/{member_id}/reject")
async def reject_a2a_group_member(
    agent_id: uuid.UUID,
    group_id: uuid.UUID,
    member_id: uuid.UUID,
    data: CollaborationGroupMemberUpdateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    source_agent, _access_level = await check_agent_access(db, current_user, agent_id)
    member = await _load_group_member_or_404(db, source_agent=source_agent, group_id=group_id, member_id=member_id)
    if not _can_moderate_a2a_member(current_user, member):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Target owner approval required")
    member.status = "rejected"
    member.rejected_at = datetime.now(timezone.utc)
    member.metadata_json = {**(member.metadata_json or {}), "reject_reason": data.reason}
    await db.flush()
    mark_agent_dirty(agent_id)
    mark_agent_dirty(member.agent_id)
    await db.commit()
    return {"status": "ok", "member_status": member.status}


@router.post("/groups/{group_id}/members/{member_id}/revoke")
async def revoke_a2a_group_member(
    agent_id: uuid.UUID,
    group_id: uuid.UUID,
    member_id: uuid.UUID,
    data: CollaborationGroupMemberUpdateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    source_agent, _access_level = await check_agent_access(db, current_user, agent_id)
    member = await _load_group_member_or_404(db, source_agent=source_agent, group_id=group_id, member_id=member_id)
    if not _can_moderate_a2a_member(current_user, member):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Target owner/admin access required")
    member.status = "revoked"
    member.revoked_at = datetime.now(timezone.utc)
    member.metadata_json = {**(member.metadata_json or {}), "revoke_reason": data.reason}
    await db.flush()
    mark_agent_dirty(agent_id)
    mark_agent_dirty(member.agent_id)
    await db.commit()
    return {"status": "ok", "member_status": member.status}
