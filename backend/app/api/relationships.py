"""Agent relationship management API — human + agent-to-agent."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import get_current_user
from app.core.permissions import check_agent_access
from app.database import get_db
from app.models.agent import Agent
from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember
from app.models.org import AgentRelationship, AgentAgentRelationship
from app.models.user import User
from app.services.a2a_collaboration_policy import build_a2a_collaboration_read_model, resolve_agent_owner_id
from app.services.relationships_file import (
    AGENT_RELATION_LABELS,
    RELATION_LABELS,
    write_relationships_file,
)

router = APIRouter(prefix="/agents/{agent_id}/relationships", tags=["relationships"])


# ─── Schemas ───────────────────────────────────────────


class RelationshipIn(BaseModel):
    member_id: str
    relation: str = "collaborator"
    description: str = ""


class RelationshipBatchIn(BaseModel):
    relationships: list[RelationshipIn]


class AgentRelationshipIn(BaseModel):
    target_agent_id: str
    relation: str = "collaborator"
    description: str = ""


class AgentRelationshipBatchIn(BaseModel):
    relationships: list[AgentRelationshipIn]


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


# ─── Human Relationships (existing) ───────────────────


@router.get("/")
async def get_relationships(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all human relationships for this agent."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentRelationship)
        .where(AgentRelationship.agent_id == agent_id)
        .options(selectinload(AgentRelationship.member))
    )
    rels = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "member_id": str(r.member_id),
            "relation": r.relation,
            "relation_label": RELATION_LABELS.get(r.relation, r.relation),
            "description": r.description,
            "member": {
                "name": r.member.name,
                "title": r.member.title,
                "department_path": r.member.department_path,
                "avatar_url": r.member.avatar_url,
                "email": r.member.email,
            }
            if r.member
            else None,
        }
        for r in rels
    ]


@router.put("/")
async def save_relationships(
    agent_id: uuid.UUID,
    data: RelationshipBatchIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace all human relationships for this agent."""
    agent, _access_level = await check_agent_access(db, current_user, agent_id)

    await db.execute(delete(AgentRelationship).where(AgentRelationship.agent_id == agent_id))

    for r in data.relationships:
        db.add(
            AgentRelationship(
                agent_id=agent_id,
                tenant_id=agent.tenant_id,
                member_id=uuid.UUID(r.member_id),
                relation=r.relation,
                description=r.description,
            )
        )

    await db.flush()

    # Regenerate file with both types
    await _regenerate_relationships_file(db, agent_id)
    await db.commit()
    return {"status": "ok"}


@router.delete("/{rel_id}")
async def delete_relationship(
    agent_id: uuid.UUID,
    rel_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single human relationship."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentRelationship).where(AgentRelationship.id == rel_id, AgentRelationship.agent_id == agent_id)
    )
    rel = result.scalar_one_or_none()
    if rel:
        await db.delete(rel)
        await db.flush()
        await _regenerate_relationships_file(db, agent_id)
        await db.commit()

    return {"status": "ok"}


# ─── Agent-to-Agent Relationships (new) ───────────────


@router.get("/agents")
async def get_agent_relationships(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all agent-to-agent relationships."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentAgentRelationship)
        .where(AgentAgentRelationship.agent_id == agent_id)
        .options(selectinload(AgentAgentRelationship.target_agent))
    )
    rels = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "target_agent_id": str(r.target_agent_id),
            "relation": r.relation,
            "relation_label": AGENT_RELATION_LABELS.get(r.relation, r.relation),
            "description": r.description,
            "target_agent": {
                "id": str(r.target_agent.id),
                "name": r.target_agent.name,
                "role_description": r.target_agent.role_description or "",
                "avatar_url": r.target_agent.avatar_url or "",
            }
            if r.target_agent
            else None,
        }
        for r in rels
    ]


@router.put("/agents")
async def save_agent_relationships(
    agent_id: uuid.UUID,
    data: AgentRelationshipBatchIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace all agent-to-agent relationships."""
    agent, _access_level = await check_agent_access(db, current_user, agent_id)

    await db.execute(delete(AgentAgentRelationship).where(AgentAgentRelationship.agent_id == agent_id))

    for r in data.relationships:
        target_id = uuid.UUID(r.target_agent_id)
        if target_id == agent_id:
            continue  # skip self-reference
        db.add(
            AgentAgentRelationship(
                agent_id=agent_id,
                tenant_id=agent.tenant_id,
                target_agent_id=target_id,
                relation=r.relation,
                description=r.description,
            )
        )

    await db.flush()
    await _regenerate_relationships_file(db, agent_id)
    await db.commit()
    return {"status": "ok"}


@router.delete("/agents/{rel_id}")
async def delete_agent_relationship(
    agent_id: uuid.UUID,
    rel_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single agent-to-agent relationship."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentAgentRelationship).where(
            AgentAgentRelationship.id == rel_id,
            AgentAgentRelationship.agent_id == agent_id,
        )
    )
    rel = result.scalar_one_or_none()
    if rel:
        await db.delete(rel)
        await db.flush()
        await _regenerate_relationships_file(db, agent_id)
        await db.commit()

    return {"status": "ok"}


# ─── Governed A2A Collaboration Groups ─────────────────


@router.get("/a2a-collaborators")
async def get_a2a_collaborators(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the canonical A2A collaborator projection for this agent."""
    await check_agent_access(db, current_user, agent_id)
    return await build_a2a_collaboration_read_model(db, agent_id)


@router.post("/a2a-groups")
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
    await _regenerate_relationships_file(db, agent_id)
    await db.commit()
    return {"status": "ok", "group_id": str(group.id), "group_name": group.name}


@router.post("/a2a-groups/{group_id}/members")
async def invite_a2a_group_member(
    agent_id: uuid.UUID,
    group_id: uuid.UUID,
    data: CollaborationGroupInviteIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Invite an agent into a collaboration group.

    Same-owner targets become active immediately. Cross-owner targets enter
    pending_owner_confirmation and must be approved by the target owner/admin.
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
    await _regenerate_relationships_file(db, agent_id)
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


@router.post("/a2a-groups/{group_id}/members/{member_id}/approve")
async def approve_a2a_group_member(
    agent_id: uuid.UUID,
    group_id: uuid.UUID,
    member_id: uuid.UUID,
    data: CollaborationGroupMemberUpdateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    source_agent, _access_level = await check_agent_access(db, current_user, agent_id)
    member = await db.get(AgentCollaborationGroupMember, member_id)
    if not member or member.group_id != group_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="A2A group member not found")
    group = await db.get(AgentCollaborationGroup, group_id)
    if not group or group.tenant_id != source_agent.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="A2A Collaboration Group not found")
    if not _can_moderate_a2a_member(current_user, member):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Target owner approval required")
    member.status = "active"
    member.approved_by_user_id = current_user.id
    member.approved_at = datetime.now(timezone.utc)
    member.rejected_at = None
    member.revoked_at = None
    member.metadata_json = {**(member.metadata_json or {}), "approval_reason": data.reason}
    await db.flush()
    await _regenerate_relationships_file(db, agent_id)
    await db.commit()
    return {"status": "ok", "member_status": member.status}


@router.post("/a2a-groups/{group_id}/members/{member_id}/reject")
async def reject_a2a_group_member(
    agent_id: uuid.UUID,
    group_id: uuid.UUID,
    member_id: uuid.UUID,
    data: CollaborationGroupMemberUpdateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    source_agent, _access_level = await check_agent_access(db, current_user, agent_id)
    member = await db.get(AgentCollaborationGroupMember, member_id)
    if not member or member.group_id != group_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="A2A group member not found")
    group = await db.get(AgentCollaborationGroup, group_id)
    if not group or group.tenant_id != source_agent.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="A2A Collaboration Group not found")
    if not _can_moderate_a2a_member(current_user, member):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Target owner approval required")
    member.status = "rejected"
    member.rejected_at = datetime.now(timezone.utc)
    member.metadata_json = {**(member.metadata_json or {}), "reject_reason": data.reason}
    await db.flush()
    await _regenerate_relationships_file(db, agent_id)
    await db.commit()
    return {"status": "ok", "member_status": member.status}


@router.post("/a2a-groups/{group_id}/members/{member_id}/revoke")
async def revoke_a2a_group_member(
    agent_id: uuid.UUID,
    group_id: uuid.UUID,
    member_id: uuid.UUID,
    data: CollaborationGroupMemberUpdateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    source_agent, _access_level = await check_agent_access(db, current_user, agent_id)
    member = await db.get(AgentCollaborationGroupMember, member_id)
    if not member or member.group_id != group_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="A2A group member not found")
    group = await db.get(AgentCollaborationGroup, group_id)
    if not group or group.tenant_id != source_agent.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="A2A Collaboration Group not found")
    if not _can_moderate_a2a_member(current_user, member):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Target owner/admin access required")
    member.status = "revoked"
    member.revoked_at = datetime.now(timezone.utc)
    member.metadata_json = {**(member.metadata_json or {}), "revoke_reason": data.reason}
    await db.flush()
    await _regenerate_relationships_file(db, agent_id)
    await db.commit()
    return {"status": "ok", "member_status": member.status}


# ─── relationships.md Generation ──────────────────────


async def _regenerate_relationships_file(db: AsyncSession, agent_id: uuid.UUID):
    """Regenerate relationships.md with the shared explicit-relationship writer.

    Inline write covers the editing user; mark_agent_dirty broadcasts to
    peer backend instances so their workspace caches converge.
    """
    from app.services.workspace_sync_dirty import mark_agent_dirty

    await write_relationships_file(db=db, agent_id=agent_id, include_owner=True)
    mark_agent_dirty(agent_id)
