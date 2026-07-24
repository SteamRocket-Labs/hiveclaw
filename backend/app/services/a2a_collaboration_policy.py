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

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.agent import Agent, AgentPermission
from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember
from app.services.tool_visibility import HR_AGENT_CLASS, HR_AGENT_NAME, is_hr_agent

ACTIVE_AGENT_STATUSES = {"running", "idle", "creating"}
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


def _agent_status(agent: Any | None) -> str:
    return str(getattr(agent, "status", "") or "").strip().lower()


def is_a2a_participating_agent(agent: Any | None) -> bool:
    """Return whether an agent is allowed to participate in A2A.

    The tenant System HR agent is a company-level onboarding tool, not a
    colleague identity. It must never be source, target, prompt context, or
    collaboration-group member for A2A.
    """

    return not is_hr_agent(agent)


def _a2a_agent_sql_filter():
    return ~and_(Agent.agent_class == HR_AGENT_CLASS, Agent.name == HR_AGENT_NAME)


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


async def _is_public_agent(db: AsyncSession | None, target_agent: Any) -> bool:
    """Return whether the target is currently company-callable within its tenant.

    Hive does not have a separate `is_public` agent column. The live public
    signal is the same permission surface used by normal access checks:
    `AgentPermission(scope_type="company")`.
    """

    if db is None:
        return False
    result = await db.execute(
        select(AgentPermission.id).where(
            AgentPermission.agent_id == getattr(target_agent, "id", None),
            AgentPermission.scope_type == "company",
            or_(
                AgentPermission.tenant_id == getattr(target_agent, "tenant_id", None),
                AgentPermission.tenant_id.is_(None),
            ),
        )
    )
    return result.scalar_one_or_none() is not None


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
    if not is_a2a_participating_agent(source_agent) or not is_a2a_participating_agent(target_agent):
        return A2ACollaborationPolicyResult(
            False,
            "system_hr_a2a_disabled",
            "System HR is reserved for digital employee onboarding and cannot participate in A2A collaboration.",
        )
    if getattr(source_agent, "id", None) == getattr(target_agent, "id", None):
        return A2ACollaborationPolicyResult(False, "self", "An agent cannot send A2A work to itself.")
    if getattr(source_agent, "tenant_id", None) != getattr(target_agent, "tenant_id", None):
        return A2ACollaborationPolicyResult(False, "cross_tenant", "Cross-tenant A2A is not exposed.")
    target_status = _agent_status(target_agent)
    if target_status not in ACTIVE_AGENT_STATUSES:
        status_label = target_status or "unknown"
        return A2ACollaborationPolicyResult(
            False,
            "target_unavailable",
            f"{getattr(target_agent, 'name', 'Target agent')} is currently {status_label} and unavailable for A2A.",
        )

    source_owner = resolve_agent_owner_id(source_agent)
    target_owner = resolve_agent_owner_id(target_agent)
    if source_owner is not None and source_owner == target_owner:
        return A2ACollaborationPolicyResult(
            True,
            "same_owner",
            "Allowed: same-owner agents can collaborate without an explicit A2A Collaboration Group.",
        )

    if await _is_public_agent(db, target_agent):
        return A2ACollaborationPolicyResult(
            True,
            "public_agent",
            "Allowed: public/company-visible agents can receive A2A work within the same tenant.",
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
            _a2a_agent_sql_filter(),
            or_(Agent.owner_user_id == owner_id, and_(Agent.owner_user_id.is_(None), Agent.creator_id == owner_id)),
        )
        .order_by(Agent.name.asc())
    )
    return list(result.scalars().all())


async def list_public_agents(db: AsyncSession, source_agent: Agent) -> list[Agent]:
    """List same-tenant public agents that are not already covered by same-owner."""

    owner_id = resolve_agent_owner_id(source_agent)
    same_owner_filter = False
    if owner_id is not None:
        same_owner_filter = or_(
            Agent.owner_user_id == owner_id,
            and_(Agent.owner_user_id.is_(None), Agent.creator_id == owner_id),
        )

    public_filters = [
        Agent.id != source_agent.id,
        Agent.tenant_id == source_agent.tenant_id,
        Agent.deleted_at.is_(None),
        Agent.status.in_(list(ACTIVE_AGENT_STATUSES)),
        _a2a_agent_sql_filter(),
        exists(
            select(1).where(
                AgentPermission.agent_id == Agent.id,
                AgentPermission.scope_type == "company",
                or_(AgentPermission.tenant_id == source_agent.tenant_id, AgentPermission.tenant_id.is_(None)),
            )
        ),
    ]
    if same_owner_filter is not False:
        public_filters.append(~same_owner_filter)

    result = await db.execute(select(Agent).where(*public_filters).order_by(Agent.name.asc()))
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
                Agent.status.in_(list(ACTIVE_AGENT_STATUSES)),
                _a2a_agent_sql_filter(),
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
                agent_class=agent.agent_class,
            )
            for agent, member in member_rows.all()
            if is_a2a_participating_agent(agent)
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
        return {"same_owner_agents": [], "public_agents": [], "collaboration_groups": []}
    if not is_a2a_participating_agent(source_agent):
        return {"same_owner_agents": [], "public_agents": [], "collaboration_groups": []}

    same_owner_agents = await list_same_owner_agents(db, source_agent)
    public_agents = await list_public_agents(db, source_agent)
    collaboration_groups = await list_active_collaboration_groups_for_agent(db, source_agent)
    same_owner_agents = [agent for agent in same_owner_agents if is_a2a_participating_agent(agent)]
    public_agents = [agent for agent in public_agents if is_a2a_participating_agent(agent)]
    return {
        "same_owner_agents": [serialize_agent_for_a2a(agent, relation="same_owner") for agent in same_owner_agents],
        "public_agents": [serialize_agent_for_a2a(agent, relation="public") for agent in public_agents],
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
                    if is_a2a_participating_agent(member)
                ],
            }
            for group in collaboration_groups
            if any(is_a2a_participating_agent(member) for member in group.members)
        ],
    }
