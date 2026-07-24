"""Human control-plane read models for A2A Collaboration Groups.

The runtime collaborator projection intentionally contains only agents that
are callable now.  This module is the separate management projection: it may
show pending, rejected, and revoked memberships to an authenticated manager,
but it is never injected into model context.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.agent import Agent
from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember
from app.models.user import User
from app.services.a2a_collaboration_policy import (
    ACTIVE_AGENT_STATUSES,
    is_a2a_participating_agent,
    resolve_agent_owner_id,
)
from app.services.tool_visibility import HR_AGENT_CLASS, HR_AGENT_NAME

MODERATOR_ROLES = {"platform_admin", "org_admin"}
PENDING_MEMBER_STATUS = "pending_owner_confirmation"


def is_group_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    current = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= current


def member_action_capabilities(
    current_user: Any,
    member: Any,
    *,
    group_accepting_approval: bool = True,
) -> dict[str, bool]:
    """Return server-authoritative actions for one membership.

    An organization administrator may act as the documented governance
    fallback, but a reason is mandatory when they are not the target owner.
    """

    is_owner = getattr(member, "agent_owner_user_id", None) == getattr(current_user, "id", None)
    is_admin = getattr(current_user, "role", None) in MODERATOR_ROLES
    can_moderate = is_owner or is_admin
    member_status = str(getattr(member, "status", "") or "")
    return {
        "can_approve": can_moderate and member_status == PENDING_MEMBER_STATUS and group_accepting_approval,
        "can_reject": can_moderate and member_status == PENDING_MEMBER_STATUS,
        "can_revoke": can_moderate and member_status == "active" and getattr(member, "role", None) != "owner",
        "moderation_reason_required": bool(can_moderate and is_admin and not is_owner),
    }


def _serialize_management_member(
    *,
    agent: Agent,
    member: AgentCollaborationGroupMember,
    owner_name: str | None,
    current_user: User,
    group_accepting_approval: bool,
) -> dict[str, Any]:
    capabilities = member_action_capabilities(
        current_user,
        member,
        group_accepting_approval=group_accepting_approval,
    )
    owner_relation = "you" if member.agent_owner_user_id == current_user.id else "another_owner"
    return {
        "member_id": str(member.id),
        "agent_id": str(agent.id),
        "name": agent.name,
        "role_description": agent.role_description or "",
        "agent_status": agent.status,
        "role": member.role,
        "status": member.status,
        "owner_name": owner_name or "Unknown owner",
        "owner_relation": owner_relation,
        "invitation_reason": member.invitation_reason or "",
        "capability_scope": member.capability_scope or {},
        **capabilities,
    }


async def build_a2a_group_management_read_model(
    db: AsyncSession,
    *,
    source_agent: Agent,
    current_user: User,
) -> dict[str, Any]:
    """Return groups relevant to a managed Agent, including non-callable states."""

    membership_group_ids = select(AgentCollaborationGroupMember.group_id).where(
        AgentCollaborationGroupMember.agent_id == source_agent.id
    )
    group_result = await db.execute(
        select(AgentCollaborationGroup)
        .where(
            AgentCollaborationGroup.tenant_id == source_agent.tenant_id,
            or_(
                AgentCollaborationGroup.created_by_agent_id == source_agent.id,
                AgentCollaborationGroup.id.in_(membership_group_ids),
            ),
        )
        .order_by(AgentCollaborationGroup.updated_at.desc(), AgentCollaborationGroup.id.desc())
    )

    groups: list[dict[str, Any]] = []
    for group in group_result.scalars().all():
        member_result = await db.execute(
            select(Agent, AgentCollaborationGroupMember, User.display_name)
            .join(AgentCollaborationGroupMember, AgentCollaborationGroupMember.agent_id == Agent.id)
            .outerjoin(User, User.id == AgentCollaborationGroupMember.agent_owner_user_id)
            .where(
                AgentCollaborationGroupMember.group_id == group.id,
                AgentCollaborationGroupMember.tenant_id == source_agent.tenant_id,
            )
            .order_by(AgentCollaborationGroupMember.created_at.asc(), AgentCollaborationGroupMember.id.asc())
        )
        rows = list(member_result.all())
        source_membership = next(
            (member for agent, member, _owner_name in rows if agent.id == source_agent.id),
            None,
        )
        group_accepting_approval = group.status == "active" and not is_group_expired(group.expires_at)
        can_invite = bool(
            group_accepting_approval
            and source_membership is not None
            and source_membership.status == "active"
            and is_a2a_participating_agent(source_agent)
        )
        groups.append(
            {
                "group_id": str(group.id),
                "group_name": group.name,
                "purpose": group.purpose or "",
                "status": "expired" if is_group_expired(group.expires_at) else group.status,
                "visibility": group.visibility,
                "expires_at": group.expires_at.isoformat() if group.expires_at else None,
                "can_invite": can_invite,
                "members": [
                    _serialize_management_member(
                        agent=agent,
                        member=member,
                        owner_name=owner_name,
                        current_user=current_user,
                        group_accepting_approval=group_accepting_approval,
                    )
                    for agent, member, owner_name in rows
                    if is_a2a_participating_agent(agent)
                ],
            }
        )
    return {"groups": groups}


def _candidate_invite_action(member: AgentCollaborationGroupMember | None) -> str:
    if member is None:
        return "invite"
    if member.status in {"rejected", "revoked"}:
        return "reinvite"
    if member.status == PENDING_MEMBER_STATUS:
        return "pending"
    return "already_active"


async def search_a2a_invite_candidates(
    db: AsyncSession,
    *,
    source_agent: Agent,
    group_id: uuid.UUID,
    current_user: User,
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search a bounded same-tenant directory for explicit group invitations."""

    member_alias = aliased(AgentCollaborationGroupMember)
    pattern = f"%{query.strip()}%"
    result = await db.execute(
        select(Agent, User.display_name, member_alias)
        .outerjoin(User, User.id == func.coalesce(Agent.owner_user_id, Agent.creator_id))
        .outerjoin(
            member_alias,
            and_(
                member_alias.group_id == group_id,
                member_alias.agent_id == Agent.id,
            ),
        )
        .where(
            Agent.id != source_agent.id,
            Agent.tenant_id == source_agent.tenant_id,
            Agent.deleted_at.is_(None),
            Agent.status.in_(list(ACTIVE_AGENT_STATUSES)),
            ~and_(Agent.agent_class == HR_AGENT_CLASS, Agent.name == HR_AGENT_NAME),
            or_(Agent.name.ilike(pattern), Agent.role_description.ilike(pattern)),
        )
        .order_by(Agent.name.asc(), Agent.id.asc())
        .limit(max(1, min(int(limit), 50)))
    )
    candidates: list[dict[str, Any]] = []
    for agent, owner_name, membership in result.all():
        if not is_a2a_participating_agent(agent):
            continue
        effective_owner_id = resolve_agent_owner_id(agent)
        candidates.append(
            {
                "agent_id": str(agent.id),
                "name": agent.name,
                "role_description": agent.role_description or "",
                "status": agent.status,
                "owner_name": owner_name or "Unknown owner",
                "owner_relation": "you" if effective_owner_id == current_user.id else "another_owner",
                "membership_status": membership.status if membership is not None else None,
                "invite_action": _candidate_invite_action(membership),
            }
        )
    return candidates
