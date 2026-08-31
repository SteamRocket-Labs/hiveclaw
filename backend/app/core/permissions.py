"""RBAC permission checking utilities."""

import uuid
from dataclasses import dataclass
from typing import Tuple

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.policy import check_permission
from app.database import enter_rls_bypass, pin_rls_tenant_context
from app.models.agent import Agent, AgentPermission
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason


async def check_agent_access(db: AsyncSession, user: User, agent_id: uuid.UUID) -> Tuple[Agent, str]:
    """Check if a user has access to a specific agent.

    Returns (agent, access_level) where access_level is 'manage' or 'use'.

    Access is granted if:
    1. User is org admin for the agent's tenant → manage
    2. User is the current agent owner → manage
    3. User has an applicable explicit permission → from permission record

    Platform administrators have no implicit company/department Agent access;
    they act as a user only through ownership or an exact user-scoped grant.
    """
    if user.role == "platform_admin":
        async with enter_rls_bypass(
            db,
            reason=f"platform-admin agent access lookup for {agent_id}",
            actor_id=str(user.id),
        ) as bypass_db:
            result = await bypass_db.execute(
                select(Agent)
                .options(selectinload(Agent.owner), selectinload(Agent.creator))
                .where(Agent.id == agent_id)
            )
    else:
        result = await db.execute(
            select(Agent).options(selectinload(Agent.owner), selectinload(Agent.creator)).where(Agent.id == agent_id)
        )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    lifecycle_reason = get_agent_lifecycle_block_reason(agent)
    if lifecycle_reason == "deleted":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if lifecycle_reason:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"Agent is not active: {lifecycle_reason}",
        )

    # Agents are tenant-owned resources. A legacy/corrupt tenant-less Agent
    # must never become globally reachable through either a platform role or a
    # missing-tenant equality shortcut; the R-023 migration quarantines such
    # rows and this guard remains the application-layer fail-closed boundary.
    if agent.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    if user.role == "platform_admin":
        await pin_rls_tenant_context(db, agent.tenant_id)
    elif user.tenant_id is None or user.tenant_id != agent.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Organization admins can audit and manage every agent in their own tenant,
    # including agents whose explicit permission scope is limited to one user.
    if user.role == "org_admin" and user.tenant_id and agent.tenant_id == user.tenant_id:
        return agent, "manage"

    # ``creator_id`` is immutable provenance.  The current owner is the
    # authority source; creator is used only as a legacy fallback for rows that
    # predate ``owner_user_id``.
    if effective_agent_owner_id(agent) == user.id:
        return agent, "manage"

    # Check permission scopes
    perms = await db.execute(select(AgentPermission).where(AgentPermission.agent_id == agent_id))
    permissions = perms.scalars().all()

    for perm in permissions:
        if user.role == "platform_admin" and perm.scope_type != "user":
            continue
        if perm.scope_type == "company":
            return agent, perm.access_level or "use"
        if perm.scope_type == "user" and perm.scope_id == user.id:
            return agent, perm.access_level or "use"
        if perm.scope_type == "department" and user.department_id:
            if perm.scope_id == user.department_id:
                return agent, perm.access_level or "use"

    resource_principals: list[tuple[str, uuid.UUID]] = [("user", user.id)]
    if user.role != "platform_admin" and user.department_id:
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


def effective_agent_owner_id(agent: Agent) -> uuid.UUID | None:
    """Return the current owner, falling back to creator for legacy rows."""

    return getattr(agent, "owner_user_id", None) or getattr(agent, "creator_id", None)


def can_manage_agent_sessions(access_level: str) -> bool:
    """Use the resolved Agent capability; a platform role is not an upgrade."""

    return access_level == "manage"


def agent_owned_by_clause(user_id: uuid.UUID):
    """SQL expression matching the canonical owner with legacy fallback."""

    return or_(
        Agent.owner_user_id == user_id,
        and_(Agent.owner_user_id.is_(None), Agent.creator_id == user_id),
    )


async def require_agent_manage_access(db: AsyncSession, user: User, agent_id: uuid.UUID) -> Agent:
    """Resolve an agent and require the canonical ``manage`` capability."""

    agent, access_level = await check_agent_access(db, user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manage access to this agent is required")
    return agent


async def require_agent_owner_or_admin(
    db: AsyncSession,
    user: User,
    agent_id: uuid.UUID,
    *,
    lock: bool = False,
) -> Agent:
    """Require root ownership authority, excluding delegated ``manage`` grants.

    Ownership changes are stronger than ordinary configuration management.
    They are available only to the current owner or a same-tenant org admin.
    The lookup intentionally ignores inactive-owner lifecycle blocking so an
    organization administrator can recover an orphaned Agent.
    """

    stmt = select(Agent).options(selectinload(Agent.owner), selectinload(Agent.creator)).where(Agent.id == agent_id)
    if lock:
        stmt = stmt.with_for_update()

    if user.role == "platform_admin":
        async with enter_rls_bypass(
            db,
            reason=f"platform-admin agent ownership lookup for {agent_id}",
            actor_id=str(user.id),
        ) as bypass_db:
            result = await bypass_db.execute(stmt)
    else:
        result = await db.execute(stmt)
    agent = result.scalar_one_or_none()
    if agent is None or agent.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if agent.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    if user.role == "platform_admin":
        await pin_rls_tenant_context(db, agent.tenant_id)
    elif user.tenant_id is None or user.tenant_id != agent.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if user.role == "org_admin" or effective_agent_owner_id(agent) == user.id:
        return agent
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only the current owner or an administrator can transfer Agent ownership",
    )


@dataclass(frozen=True)
class SessionActionDecision:
    """Authenticated authority decision for one user-facing session action."""

    agent: Agent
    session: ChatSession
    access_level: str
    authority_source: str
    action: str


_READ_ONLY_SESSION_KINDS = frozenset({"delegation_run"})


def require_writable_session(session: ChatSession, *, action: str) -> None:
    """Reject mutation of product read-only Session kinds.

    This is an exact machine-contract gate.  It deliberately does not infer
    mutability from titles, messages, or other natural-language content.
    """

    session_kind = str(getattr(session, "session_kind", "") or "").strip().lower()
    if session_kind not in _READ_ONLY_SESSION_KINDS:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "session_read_only",
            "session_kind": session_kind,
            "action": str(action),
        },
    )


async def authorize_session_action(
    db: AsyncSession,
    user: User,
    *,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    action: str,
    allow_manager_override: bool = False,
    manager_override_reason: str | None = None,
    require_writable: bool = False,
) -> SessionActionDecision:
    """Bind an action to both Agent authority and the session's user.

    Ordinary Agent ``use``/``manage`` access never grants access to another
    user's session.  A manager may cross that boundary only through a caller
    that explicitly enables the override and supplies an auditable reason.
    """

    agent, access_level = await check_agent_access(db, user, agent_id)
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.agent_id == agent_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found")
    if str(session.user_id) == str(user.id):
        if require_writable:
            require_writable_session(session, action=action)
        return SessionActionDecision(
            agent=agent,
            session=session,
            access_level=access_level,
            authority_source="session_owner",
            action=action,
        )

    reason = str(manager_override_reason or "").strip()
    if access_level == "manage" and allow_manager_override and reason:
        if require_writable:
            require_writable_session(session, action=action)
        from app.services.audit_logger import write_audit_log

        await write_audit_log(
            "session_authority_override",
            details={
                "session_id": str(session.id),
                "session_user_id": str(session.user_id),
                "action": action,
                "reason": reason,
                "authority_source": "manager_override",
            },
            agent_id=agent_id,
            user_id=user.id,
        )
        return SessionActionDecision(
            agent=agent,
            session=session,
            access_level=access_level,
            authority_source="manager_override",
            action=action,
        )

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This session belongs to a different user")


def is_agent_expired(agent: Agent) -> bool:
    """Return True when lifecycle policy blocks execution."""
    return get_agent_lifecycle_block_reason(agent) is not None
