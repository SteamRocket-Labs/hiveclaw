"""RBAC permission checking utilities."""

import uuid
from typing import Tuple

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.policy import check_permission
from app.models.agent import Agent, AgentPermission
from app.models.user import User


async def check_agent_access(db: AsyncSession, user: User, agent_id: uuid.UUID) -> Tuple[Agent, str]:
    """Check if a user has access to a specific agent.

    Returns (agent, access_level) where access_level is 'manage' or 'use'.

    Access is granted if:
    1. User is platform admin → manage
    2. User is org admin for the agent's tenant → manage
    3. User is the agent creator → manage
    4. User has explicit permission (company/user scope) → from permission record
    """
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Platform admins can access everything with manage
    if user.role == "platform_admin":
        return agent, "manage"

    # Tenant boundary: non-platform users can only access agents in their own tenant
    if user.tenant_id and agent.tenant_id and user.tenant_id != agent.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Organization admins can audit and manage every agent in their own tenant,
    # including agents whose explicit permission scope is limited to one user.
    if user.role == "org_admin" and user.tenant_id and agent.tenant_id == user.tenant_id:
        return agent, "manage"

    # Creator always has manage access
    if agent.creator_id == user.id:
        return agent, "manage"

    # Check permission scopes
    perms = await db.execute(select(AgentPermission).where(AgentPermission.agent_id == agent_id))
    permissions = perms.scalars().all()

    for perm in permissions:
        if perm.scope_type == "company":
            return agent, perm.access_level or "use"
        if perm.scope_type == "user" and perm.scope_id == user.id:
            return agent, perm.access_level or "use"
        if perm.scope_type == "department" and user.department_id:
            if perm.scope_id == user.department_id:
                return agent, perm.access_level or "use"

    resource_principals: list[tuple[str, uuid.UUID]] = [("user", user.id)]
    if user.department_id:
        resource_principals.append(("department", user.department_id))

    for action, access_level in (("manage", "manage"), ("execute", "use"), ("read", "use")):
        for principal_type, principal_id in resource_principals:
            try:
                allowed = await check_permission(
                    db,
                    principal_type=principal_type,
                    principal_id=principal_id,
                    resource_type="agent",
                    resource_id=agent_id,
                    action=action,
                    context={"tenant_id": str(user.tenant_id) if user.tenant_id else None},
                )
            except Exception:
                allowed = False
            if allowed:
                return agent, access_level

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this agent")


def is_agent_creator(user: User, agent: Agent) -> bool:
    """Check if the user is the creator (admin) of the agent."""
    return agent.creator_id == user.id or user.role == "platform_admin"


def is_agent_expired(agent: Agent) -> bool:
    """Agent expiry has been removed — always returns False."""
    return False
