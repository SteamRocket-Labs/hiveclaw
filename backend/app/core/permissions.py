"""RBAC permission checking utilities."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Tuple

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.policy import check_permission
from app.database import enter_rls_bypass, get_current_tenant_id, pin_rls_tenant_context
from app.models.agent import Agent, AgentPermission
from app.models.chat_session import ChatSession
from app.models.security_audit import ResourcePermission
from app.models.tenant import Tenant
from app.models.user import User
from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason


def _platform_admin_selected_tenant_id(user) -> uuid.UUID | None:
    """The one selected company a platform administrator acts inside (PDEC-013).

    The bypass Agent lookup may locate an exact row in any company; business
    authority exists only inside the already authenticated/pinned selected
    tenant — the ``X-Tenant-Id`` channel validated by ``get_current_user``
    (or the pinned scope of a background ``tenant_scoped_session``/durable
    recovery lane, which pins the command's validated tenant before loaders
    run). A raw Agent UUID, query parameter, or stale token is never a second
    selector, and a tenantless platform identity must select a company first.
    """

    pinned = get_current_tenant_id()
    selected = pinned or getattr(user, "tenant_id", None)
    if not selected:
        return None
    try:
        return uuid.UUID(str(selected))
    except (TypeError, ValueError):
        return None


def _platform_admin_selection_allows_agent(user, agent_tenant_id) -> bool:
    """One shared boundary for both cross-tenant platform-admin Agent loaders."""

    selected_tenant_id = _platform_admin_selected_tenant_id(user)
    return selected_tenant_id is not None and str(selected_tenant_id) == str(agent_tenant_id)


async def _load_agent_for_user(db: AsyncSession, user: User, agent_id: uuid.UUID) -> Agent:
    """Load an active Agent after the exact tenant/RLS boundary is pinned."""

    # The lookup joins the Agent's Tenant liveness so an Agent whose company
    # is retired or missing resolves exactly like a missing row — the same
    # 404, without leaking that the Agent exists. The join must stay inside
    # the platform-admin bypass scope: outside it, the request GUC only
    # exposes the caller's own Tenant row.
    if user.role == "platform_admin":
        async with enter_rls_bypass(
            db,
            reason=f"platform-admin agent access lookup for {agent_id}",
            actor_id=str(user.id),
        ) as bypass_db:
            result = await bypass_db.execute(
                select(Agent)
                .options(selectinload(Agent.owner), selectinload(Agent.creator))
                .join(Tenant, and_(Tenant.id == Agent.tenant_id, Tenant.is_active.is_(True)))
                .where(Agent.id == agent_id)
            )
    else:
        result = await db.execute(
            select(Agent)
            .options(selectinload(Agent.owner), selectinload(Agent.creator))
            .join(Tenant, and_(Tenant.id == Agent.tenant_id, Tenant.is_active.is_(True)))
            .where(Agent.id == agent_id)
        )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Selected-company boundary: the bypass lookup only locates the exact
    # row; an Agent outside the authenticated selected company resolves
    # exactly like a missing row, before any lifecycle detail is evaluated.
    if user.role == "platform_admin" and not _platform_admin_selection_allows_agent(
        user, getattr(agent, "tenant_id", None)
    ):
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

    return agent


SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE = "scoped_business_admin"


def is_scoped_business_admin(user, *, resource_tenant_id) -> bool:
    """PDEC-013 three-role contract: administrator business authority in scope.

    Company administrators hold full business authority inside their own
    company; platform administrators hold it inside the company an exact
    resource (or an explicit selection) resolves to. The role is always read
    from the canonical live ``User`` row that the request or recovery path
    loaded — never from a token claim, an ``AgentPermission`` row, or a
    ``ResourcePermission`` grant, which are delegated capabilities rather
    than administrator identity. Plaintext credentials stay excluded at
    their own boundary; this predicate only decides business scope.

    Precondition: the predicate consumes a ``resource_tenant_id`` that a caller
    upstream already resolved — the exact Agent lookup with its live-Tenant
    join, or the authenticated/pinned selected company. It performs no tenant
    lookup of its own and must never become a second tenant-selection channel;
    callers that only have an unvalidated tenant hint must resolve scope first.
    For platform administrators the selected-company equality is enforced here
    through the same boundary the cross-tenant Agent loaders use
    (:func:`_platform_admin_selected_tenant_id`), so an unselected or
    foreign-selected platform identity has no business scope even if a raw
    resource tenant reached this predicate.
    """

    role = getattr(user, "role", None)
    if role == "org_admin":
        home_tenant_id = getattr(user, "tenant_id", None)
        return (
            home_tenant_id is not None
            and resource_tenant_id is not None
            and str(home_tenant_id) == str(resource_tenant_id)
        )
    if role == "platform_admin":
        # PDEC-013: the platform administrator's business authority exists
        # only inside the one authenticated selected company — the same
        # boundary the cross-tenant Agent loaders enforce. A tenantless
        # platform identity, or one whose selection is a different company
        # than the resource, has no business scope here.
        selected_tenant_id = _platform_admin_selected_tenant_id(user)
        return (
            selected_tenant_id is not None
            and resource_tenant_id is not None
            and str(selected_tenant_id) == str(resource_tenant_id)
        )
    return False


async def check_agent_access(db: AsyncSession, user: User, agent_id: uuid.UUID) -> Tuple[Agent, str]:
    """Check whether a user may use or manage a specific Agent.

    Operator inspection is deliberately excluded. Read-only operator surfaces
    must call :func:`check_agent_operator_reachability` explicitly.
    """

    agent = await _load_agent_for_user(db, user, agent_id)

    # Scoped administrators (PDEC-013): organization administrators manage
    # every agent in their own tenant, platform administrators every agent
    # of the authenticated selected company the exact lookup resolved to,
    # regardless of any explicit permission row. The returned ``manage``
    # level is a capability, not a role: a legacy grant can also produce
    # ``manage`` for an ordinary user, so role-dependent decisions must use
    # :func:`is_scoped_business_admin`.
    if is_scoped_business_admin(user, resource_tenant_id=agent.tenant_id):
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
            # A company-wide grant is deliberately an execution/read surface,
            # never a delegated configuration-administration capability.
            # Older rows may contain ``manage``; neutralize them at the live
            # authority boundary without requiring a destructive migration.
            return agent, "use"
        if perm.scope_type == "user" and perm.scope_id == user.id:
            return agent, perm.access_level or "use"
        if perm.scope_type == "department" and user.department_id:
            if perm.scope_id == user.department_id:
                return agent, perm.access_level or "use"

    resource_principals: list[tuple[str, uuid.UUID]] = [("user", user.id)]
    if user.role != "platform_admin" and user.department_id:
        resource_principals.append(("department", user.department_id))

    primary_principal, *additional_principals = resource_principals
    for action, access_level in (("manage", "manage"), ("execute", "use"), ("read", "use")):
        try:
            allowed = await check_permission(
                db,
                principal_type=primary_principal[0],
                principal_id=primary_principal[1],
                additional_principals=additional_principals,
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


AGENT_OPERATOR_INSPECT_ACTION = "operator.inspect"
AGENT_OPERATOR_INSPECTION_SCHEMA = "hive.agent.operator_inspection.v1"


def is_agent_operator_inspection_grant(permission: ResourcePermission) -> bool:
    """Recognize only grants created through the governed operator contract."""

    conditions = getattr(permission, "conditions", None)
    metadata = conditions.get("operator_inspection") if isinstance(conditions, dict) else None
    return (
        isinstance(conditions, dict)
        and set(conditions) == {"operator_inspection"}
        and AGENT_OPERATOR_INSPECT_ACTION in (getattr(permission, "actions", None) or [])
        and isinstance(metadata, dict)
        and metadata.get("schema") == AGENT_OPERATOR_INSPECTION_SCHEMA
    )


def _operator_permission_is_live(permission: ResourcePermission, *, now: datetime) -> bool:
    if not is_agent_operator_inspection_grant(permission):
        return False
    if getattr(permission, "revoked_at", None) is not None:
        return False
    expires_at = getattr(permission, "expires_at", None)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            return False
    return True


async def _load_agent_operator_permissions(
    db: AsyncSession,
    *,
    user: User,
    agent_ids: list[uuid.UUID] | None,
) -> list[ResourcePermission]:
    if agent_ids == []:
        return []
    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id is None:
        return []
    statement = select(ResourcePermission).where(
        ResourcePermission.tenant_id == tenant_id,
        ResourcePermission.principal_type == "user",
        ResourcePermission.principal_id == user.id,
        ResourcePermission.resource_type == "agent",
    )
    if agent_ids is not None:
        statement = statement.where(ResourcePermission.resource_id.in_(agent_ids))
    result = await db.execute(statement)
    return list(result.scalars().all())


async def load_agent_operator_inspection_ids(
    db: AsyncSession,
    *,
    user: User,
    agent_ids: list[uuid.UUID] | None,
) -> set[uuid.UUID]:
    """Return Agents with a live explicit ``operator.inspect`` allow.

    Active deny grants win over active allows. Expired and revoked rows have no
    effect. This capability is intentionally independent from Agent ``manage``.
    """

    now = datetime.now(timezone.utc)
    decisions: dict[uuid.UUID, set[str]] = {}
    for permission in await _load_agent_operator_permissions(db, user=user, agent_ids=agent_ids):
        resource_id = getattr(permission, "resource_id", None)
        if resource_id is None or not _operator_permission_is_live(permission, now=now):
            continue
        decisions.setdefault(resource_id, set()).add(str(getattr(permission, "effect", "allow") or "allow"))
    return {agent_id for agent_id, effects in decisions.items() if "allow" in effects and "deny" not in effects}


async def has_agent_operator_inspect(
    db: AsyncSession,
    *,
    user: User,
    agent_id: uuid.UUID,
) -> bool:
    return agent_id in await load_agent_operator_inspection_ids(db, user=user, agent_ids=[agent_id])


async def check_agent_operator_reachability(
    db: AsyncSession,
    user: User,
    agent_id: uuid.UUID,
) -> Tuple[Agent, str]:
    """Resolve a read-only Agent shell without widening generic Agent access."""

    try:
        return await check_agent_access(db, user, agent_id)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_403_FORBIDDEN:
            raise

    agent = await _load_agent_for_user(db, user, agent_id)
    if await has_agent_operator_inspect(db, user=user, agent_id=agent_id):
        return agent, "operator"
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this agent")


async def authorize_agent_operator_inspection(
    db: AsyncSession,
    *,
    user: User,
    agent: Agent,
    reason: str | None,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    details: dict | None = None,
) -> str:
    """Authorize and audit one cross-user, read-only inspection.

    Callers reach this helper only from an explicitly read-only route. The
    audit row is written through the request transaction and is deliberately
    not best-effort: an audit failure denies the inspection.
    """

    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Operator View requires an audit reason")
    if len(normalized_reason) > 1000:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Operator reason is too long")
    if not await has_agent_operator_inspect(db, user=user, agent_id=agent.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Active operator.inspect permission is required",
        )

    from app.core.policy import write_audit_event

    await write_audit_event(
        db,
        event_type="agent.operator_inspection",
        severity="info",
        actor_type="user",
        actor_id=user.id,
        tenant_id=agent.tenant_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details={
            **(details or {}),
            "agent_id": str(agent.id),
            "reason": normalized_reason,
            "authority_source": "operator_inspect_grant",
        },
    )
    return "operator_inspect_grant"


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
    They are available only to the current owner or a scoped business
    administrator of the Agent's live company (PDEC-013): an organization
    administrator inside their own tenant, a platform administrator inside
    the authenticated selected company — enforced by the shared
    ``_platform_admin_selection_allows_agent`` boundary before the bypass
    lookup result is returned or the transaction is repinned.
    The lookup intentionally ignores inactive-owner lifecycle blocking so an
    administrator can recover an orphaned Agent — but only while
    the Agent's Tenant is live: an Agent whose company is retired or missing
    resolves exactly like a missing row, including through the platform-admin
    bypass lookup, so generic owner/admin management cannot mutate a retired
    company's Agent. The join must stay inside the bypass scope; outside it,
    the request GUC only exposes the caller's own Tenant row.
    """

    stmt = (
        select(Agent)
        .options(selectinload(Agent.owner), selectinload(Agent.creator))
        .join(Tenant, and_(Tenant.id == Agent.tenant_id, Tenant.is_active.is_(True)))
        .where(Agent.id == agent_id)
    )
    if lock:
        # Lock only the Agent row: the liveness join must not widen the row
        # lock to the Tenant identity row, which the retirement path locks
        # first — a widened FOR UPDATE could invert that order and deadlock.
        stmt = stmt.with_for_update(of=Agent)

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

    # Selected-company boundary (shared with _load_agent_for_user): the
    # bypass lookup only locates the row; repinning to a foreign company is
    # never allowed without the authenticated selection.
    if user.role == "platform_admin" and not _platform_admin_selection_allows_agent(
        user, getattr(agent, "tenant_id", None)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if agent.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    if user.role == "platform_admin":
        await pin_rls_tenant_context(db, agent.tenant_id)
    elif user.tenant_id is None or user.tenant_id != agent.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if is_scoped_business_admin(user, resource_tenant_id=agent.tenant_id) or effective_agent_owner_id(agent) == user.id:
        return agent
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only the current owner or a scoped company administrator may perform this action",
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


async def _audit_scoped_business_admin_session_access(
    db: AsyncSession,
    *,
    user: User,
    agent: Agent,
    session: ChatSession,
    action: str,
) -> None:
    """Record one administrator business access to another user's session."""

    from app.core.policy import write_audit_event

    await write_audit_event(
        db,
        event_type="session.scoped_business_admin_access",
        severity="info",
        actor_type="user",
        actor_id=user.id,
        tenant_id=agent.tenant_id,
        action=action,
        resource_type="chat_session",
        resource_id=session.id,
        details={
            "agent_id": str(agent.id),
            "session_user_id": str(session.user_id),
            "actor_role": str(getattr(user, "role", "") or ""),
            "authority_source": SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE,
            "outcome": "allowed",
        },
    )


async def authorize_loaded_session_access(
    db: AsyncSession,
    user: User,
    *,
    agent: Agent,
    session: ChatSession,
    access_level: str,
    action: str,
    operator_view: bool = False,
    operator_reason: str | None = None,
    require_writable: bool = False,
    audit: bool = True,
) -> str:
    """Authorize an already-loaded session for one actor.

    This is the single owner/admin/operator decision shared by the HTTP layer
    and the durable command lane so the two can never disagree (PDEC-013):
    the session owner always passes; a scoped business administrator passes
    without a manual operator reason or grant, attributed and audited as the
    real actor; everyone else needs the audited ``operator.inspect`` lane for
    cross-user reads and is denied cross-user mutations.
    """

    if str(session.user_id) == str(user.id):
        if require_writable:
            require_writable_session(session, action=action)
        return "session_owner"

    if is_scoped_business_admin(user, resource_tenant_id=getattr(agent, "tenant_id", None)):
        # Administrators act as themselves on ordinary writable sessions; the
        # intrinsically read-only session kinds stay read-only for them too.
        if require_writable:
            require_writable_session(session, action=action)
        if audit:
            await _audit_scoped_business_admin_session_access(
                db,
                user=user,
                agent=agent,
                session=session,
                action=action,
            )
        return SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE

    if operator_view is True and not require_writable:
        return await authorize_agent_operator_inspection(
            db,
            user=user,
            agent=agent,
            reason=operator_reason,
            action=action,
            resource_type="chat_session",
            resource_id=session.id,
            details={"session_user_id": str(session.user_id)},
        )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This session belongs to a different user")


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
    audit: bool = True,
) -> SessionActionDecision:
    """Bind an action to both Agent authority and the session's user.

    Ordinary Agent ``use``/``manage`` access never grants access to another
    user's session — a legacy ``manage`` grant is a delegated capability, not
    administrator identity. Cross-user access requires either scoped business
    administrator authority (PDEC-013, audited as the real actor) or, for
    read-only inspection, an independent live ``operator.inspect`` grant plus
    an auditable reason.
    """

    if allow_manager_override and not require_writable:
        agent, access_level = await check_agent_operator_reachability(db, user, agent_id)
    else:
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
    authority_source = await authorize_loaded_session_access(
        db,
        user,
        agent=agent,
        session=session,
        access_level=access_level,
        action=action,
        operator_view=allow_manager_override,
        operator_reason=manager_override_reason,
        require_writable=require_writable,
        audit=audit,
    )
    return SessionActionDecision(
        agent=agent,
        session=session,
        access_level=access_level,
        authority_source=authority_source,
        action=action,
    )


def is_agent_expired(agent: Agent) -> bool:
    """Return True when lifecycle policy blocks execution."""
    return get_agent_lifecycle_block_reason(agent) is not None
