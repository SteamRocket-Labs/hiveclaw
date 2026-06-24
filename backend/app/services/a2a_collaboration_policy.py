"""Governed A2A collaboration policy and read models.

Same-owner agents may collaborate implicitly. Cross-owner collaboration is
allowed only through an active A2A Collaboration Group with active membership
for both source and target agents.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.agent import Agent
from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember

ACTIVE_AGENT_STATUSES = {"running", "idle", "creating"}
BLOCKED_AGENT_STATUSES = {"expired", "stopped", "archived", "error"}
ACTIVE_GROUP_STATUS = "active"
ACTIVE_MEMBER_STATUS = "active"


@dataclass(slots=True)
class A2ACollaborationPolicyResult:
    allowed: bool
    reason: str
    message: str
    approval_required: bool = False
    group_id: uuid.UUID | None = None
    group_name: str | None = None


def resolve_agent_owner_id(agent: Any | None) -> uuid.UUID | None:
    """Return the effective human owner for an agent.

    `owner_user_id` is the explicit owner; old rows fall back to `creator_id`.
    """

    if agent is None:
        return None
    return getattr(agent, "owner_user_id", None) or getattr(agent, "creator_id", None)


def _is_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    current = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= current


async def _find_active_collaboration_edge(
    db: AsyncSession | None,
    source_agent: Any,
    target_agent: Any,
) -> SimpleNamespace | None:
    """Find a collaboration edge and return its strongest status.

    The caller deliberately passes `db=None` in unit tests when this function is
    monkeypatched. Without a database the safe default is no edge.
    """

    if db is None:
        return None

    source_member = aliased(AgentCollaborationGroupMember)
    target_member = aliased(AgentCollaborationGroupMember)
    result = await db.execute(
        select(AgentCollaborationGroup, source_member, target_member)
        .join(source_member, source_member.group_id == AgentCollaborationGroup.id)
        .join(target_member, target_member.group_id == AgentCollaborationGroup.id)
        .where(
            AgentCollaborationGroup.tenant_id == getattr(source_agent, "tenant_id", None),
            source_member.agent_id == getattr(source_agent, "id", None),
            target_member.agent_id == getattr(target_agent, "id", None),
        )
        .order_by(AgentCollaborationGroup.updated_at.desc())
    )
    rows = result.all()
    if not rows:
        return None

    best: SimpleNamespace | None = None
    for group, source_edge, target_edge in rows:
        group_status = "expired" if _is_expired(group.expires_at) else group.status
        member_statuses = {source_edge.status, target_edge.status}
        if group_status == ACTIVE_GROUP_STATUS and member_statuses == {ACTIVE_MEMBER_STATUS}:
            return SimpleNamespace(group_id=group.id, group_name=group.name, status=ACTIVE_GROUP_STATUS)
        if "pending_owner_confirmation" in member_statuses:
            status = "pending_owner_confirmation"
        elif "revoked" in member_statuses or group_status == "revoked":
            status = "revoked"
        elif "rejected" in member_statuses or group_status == "rejected":
            status = "rejected"
        else:
            status = group_status or "inactive"
        best = SimpleNamespace(group_id=group.id, group_name=group.name, status=status)
    return best


async def resolve_a2a_collaboration_policy(
    db: AsyncSession | None,
    source_agent: Any | None,
    target_agent: Any | None,
    *,
    action: str,
) -> A2ACollaborationPolicyResult:
    """Resolve whether one agent may contact/delegate to another agent."""

    if source_agent is None:
        return A2ACollaborationPolicyResult(False, "source_not_found", "Source agent not found.")
    if target_agent is None:
        return A2ACollaborationPolicyResult(False, "target_not_found", "Target agent not found.")
    if getattr(source_agent, "id", None) == getattr(target_agent, "id", None):
        return A2ACollaborationPolicyResult(False, "self", "An agent cannot send A2A work to itself.")
    if getattr(source_agent, "tenant_id", None) != getattr(target_agent, "tenant_id", None):
        return A2ACollaborationPolicyResult(False, "cross_tenant", "Cross-tenant A2A is not exposed.")
    if str(getattr(target_agent, "status", "") or "") in BLOCKED_AGENT_STATUSES:
        return A2ACollaborationPolicyResult(
            False,
            "target_unavailable",
            f"{getattr(target_agent, 'name', 'Target agent')} is unavailable for A2A.",
        )

    source_owner = resolve_agent_owner_id(source_agent)
    target_owner = resolve_agent_owner_id(target_agent)
    if source_owner is not None and source_owner == target_owner:
        return A2ACollaborationPolicyResult(
            True,
            "same_owner",
            "Allowed: same-owner agents can collaborate without an explicit A2A Collaboration Group.",
        )

    edge = await _find_active_collaboration_edge(db, source_agent, target_agent)
    if edge and edge.status == ACTIVE_GROUP_STATUS:
        return A2ACollaborationPolicyResult(
            True,
            "active_group",
            f"Allowed by active A2A Collaboration Group: {edge.group_name}.",
            group_id=edge.group_id,
            group_name=edge.group_name,
        )
    if edge:
        return A2ACollaborationPolicyResult(
            False,
            str(edge.status),
            (
                f"Blocked by A2A Collaboration Group '{edge.group_name}' status={edge.status}. "
                "Owner approval is required before cross-owner A2A can run."
            ),
            approval_required=edge.status == "pending_owner_confirmation",
            group_id=edge.group_id,
            group_name=edge.group_name,
        )
    return A2ACollaborationPolicyResult(
        False,
        "no_group",
        (
            "Cross-owner A2A requires an active A2A Collaboration Group with both agents approved. "
            "Create or approve a group membership before retrying."
        ),
        approval_required=True,
    )


def serialize_agent_for_a2a(agent: Agent, *, relation: str) -> dict[str, Any]:
    return {
        "id": str(agent.id),
        "name": agent.name,
        "role_description": agent.role_description or "",
        "status": agent.status,
        "agent_type": getattr(agent, "agent_type", "native"),
        "owner_user_id": str(resolve_agent_owner_id(agent) or ""),
        "relation": relation,
    }


async def list_same_owner_agents(db: AsyncSession, source_agent: Agent) -> list[Agent]:
    owner_id = resolve_agent_owner_id(source_agent)
    if owner_id is None:
        return []

    result = await db.execute(
        select(Agent)
        .where(
            Agent.id != source_agent.id,
            Agent.tenant_id == source_agent.tenant_id,
            Agent.deleted_at.is_(None),
            Agent.status.in_(list(ACTIVE_AGENT_STATUSES)),
            or_(Agent.owner_user_id == owner_id, and_(Agent.owner_user_id.is_(None), Agent.creator_id == owner_id)),
        )
        .order_by(Agent.name.asc())
    )
    return list(result.scalars().all())


async def list_active_collaboration_groups_for_agent(db: AsyncSession, source_agent: Agent) -> list[SimpleNamespace]:
    source_memberships = await db.execute(
        select(AgentCollaborationGroup, AgentCollaborationGroupMember)
        .join(AgentCollaborationGroupMember, AgentCollaborationGroupMember.group_id == AgentCollaborationGroup.id)
        .where(
            AgentCollaborationGroup.tenant_id == source_agent.tenant_id,
            AgentCollaborationGroup.status == ACTIVE_GROUP_STATUS,
            AgentCollaborationGroupMember.agent_id == source_agent.id,
            AgentCollaborationGroupMember.status == ACTIVE_MEMBER_STATUS,
        )
        .order_by(AgentCollaborationGroup.updated_at.desc())
    )
    groups: list[SimpleNamespace] = []
    for group, _membership in source_memberships.all():
        if _is_expired(group.expires_at):
            continue
        member_rows = await db.execute(
            select(Agent, AgentCollaborationGroupMember)
            .join(AgentCollaborationGroupMember, AgentCollaborationGroupMember.agent_id == Agent.id)
            .where(
                AgentCollaborationGroupMember.group_id == group.id,
                AgentCollaborationGroupMember.status == ACTIVE_MEMBER_STATUS,
                Agent.id != source_agent.id,
                Agent.deleted_at.is_(None),
            )
            .order_by(Agent.name.asc())
        )
        members = [
            SimpleNamespace(
                agent_id=agent.id,
                name=agent.name,
                role_description=agent.role_description or "",
                status=member.status,
                role=member.role,
                owner_user_id=member.agent_owner_user_id,
            )
            for agent, member in member_rows.all()
        ]
        if members:
            groups.append(
                SimpleNamespace(
                    group_id=group.id,
                    group_name=group.name,
                    purpose=group.purpose or "",
                    status=group.status,
                    members=members,
                )
            )
    return groups


async def build_a2a_collaboration_read_model(db: AsyncSession, agent_id: uuid.UUID) -> dict[str, Any]:
    source_agent = await db.get(Agent, agent_id)
    if source_agent is None:
        return {"same_owner_agents": [], "collaboration_groups": []}

    same_owner_agents = await list_same_owner_agents(db, source_agent)
    collaboration_groups = await list_active_collaboration_groups_for_agent(db, source_agent)
    return {
        "same_owner_agents": [serialize_agent_for_a2a(agent, relation="same_owner") for agent in same_owner_agents],
        "collaboration_groups": [
            {
                "group_id": str(group.group_id),
                "group_name": group.group_name,
                "purpose": group.purpose,
                "status": group.status,
                "members": [
                    {
                        "agent_id": str(member.agent_id),
                        "id": str(member.agent_id),
                        "name": member.name,
                        "role_description": member.role_description,
                        "status": member.status,
                        "role": member.role,
                        "owner_user_id": str(member.owner_user_id),
                    }
                    for member in group.members
                ],
            }
            for group in collaboration_groups
        ],
    }
