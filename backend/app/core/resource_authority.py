"""Canonical authority boundary for user-owned Agent resources.

Agent access answers whether a principal may execute an Agent.  It deliberately
does not answer whether that principal owns a file, artifact, task, schedule,
or activity record produced through the Agent.  This module is the one place
that joins those two decisions.
"""

from __future__ import annotations

import posixpath
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE,
    authorize_agent_operator_inspection,
    check_agent_access,
    check_agent_operator_reachability,
    is_scoped_business_admin,
)
from app.core.policy import check_permission, permission_effect_applies
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.security_audit import ResourcePermission
from app.models.user import User


_WORKSPACE_RESOURCE_NAMESPACE = uuid.UUID("11419440-9c58-4ec4-8bb4-e4c9f7ed9a7e")
OWNED_AUTHORITY_STATE = "owned"
QUARANTINED_AUTHORITY_STATE = "quarantined"


def normalize_workspace_resource_path(path: str) -> str:
    """Return a stable POSIX resource path without accepting parent traversal."""

    raw = str(path or "").replace("\\", "/").strip().lstrip("/")
    normalized = posixpath.normpath(raw)
    if normalized in {"", "."}:
        return ""
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("workspace resource path cannot escape the Agent root")
    return normalized


def workspace_resource_id(agent_id: uuid.UUID, path: str) -> uuid.UUID:
    """Build the UUID used by generic resource grants for a workspace path."""

    normalized = normalize_workspace_resource_path(path)
    return uuid.uuid5(_WORKSPACE_RESOURCE_NAMESPACE, f"{agent_id}:{normalized}")


@dataclass(frozen=True)
class ResourceAuthorityDecision:
    """One authenticated, auditable resource authorization decision."""

    agent: Agent
    access_level: str
    resource_kind: str
    resource_id: uuid.UUID
    action: str
    authority_source: str
    operator_view: bool = False


async def _root_session_owned_by_user(
    db: AsyncSession,
    *,
    root_session_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    if root_session_id is None:
        return False
    result = await db.execute(
        select(ChatSession.id).where(
            ChatSession.id == root_session_id,
            ChatSession.agent_id == agent_id,
            ChatSession.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def _has_explicit_resource_grant(
    db: AsyncSession,
    *,
    user: User,
    resource_kind: str,
    resource_id: uuid.UUID,
    action: str,
) -> bool:
    principals: list[tuple[str, uuid.UUID]] = [("user", user.id)]
    if getattr(user, "role", None) != "platform_admin" and getattr(user, "department_id", None):
        principals.append(("department", user.department_id))
    context = {"tenant_id": str(user.tenant_id) if getattr(user, "tenant_id", None) else None}
    primary_principal, *additional_principals = principals
    return await check_permission(
        db,
        principal_type=primary_principal[0],
        principal_id=primary_principal[1],
        additional_principals=additional_principals,
        resource_type=resource_kind,
        resource_id=resource_id,
        action=action,
        context=context,
    )


async def load_explicit_resource_grant_ids(
    db: AsyncSession,
    *,
    user: User,
    resource_kind: str,
    action: str,
) -> set[uuid.UUID]:
    """Load all grants for one principal/resource kind in one bounded query.

    List surfaces use this once and then push the resulting IDs into their SQL
    visibility predicate. That preserves the canonical ABAC evaluator while
    eliminating the previous one-query-per-row authorization loop.
    """

    principal_clauses = [and_(ResourcePermission.principal_type == "user", ResourcePermission.principal_id == user.id)]
    department_id = getattr(user, "department_id", None)
    if getattr(user, "role", None) != "platform_admin" and department_id:
        principal_clauses.append(
            and_(
                ResourcePermission.principal_type == "department",
                ResourcePermission.principal_id == department_id,
            )
        )
    statement = select(ResourcePermission).where(
        ResourcePermission.resource_type == resource_kind,
        or_(*principal_clauses),
    )
    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id is not None:
        statement = statement.where(ResourcePermission.tenant_id == tenant_id)
    permissions = (await db.execute(statement)).scalars().all()
    context = {"tenant_id": str(tenant_id) if tenant_id else None}
    effects_by_resource: dict[uuid.UUID, set[str]] = {}
    for permission in permissions:
        if permission.resource_id is None or not permission_effect_applies(
            permission,
            action=action,
            context=context,
        ):
            continue
        effects_by_resource.setdefault(permission.resource_id, set()).add(
            str(getattr(permission, "effect", "allow") or "allow")
        )
    return {
        resource_id
        for resource_id, effects in effects_by_resource.items()
        if "allow" in effects and "deny" not in effects
    }


async def _audit_scoped_business_admin_resource_access(
    db: AsyncSession,
    *,
    user: User,
    agent: Agent,
    decision_resource_kind: str,
    decision_resource_id: uuid.UUID,
    action: str,
    owner_user_id: uuid.UUID | None,
    root_session_id: uuid.UUID | None,
    operator_view_requested: bool,
    details_extra: dict | None = None,
) -> None:
    """Record one administrator business access to another principal's resource."""

    from app.core.policy import write_audit_event

    await write_audit_event(
        db,
        event_type="resource.scoped_business_admin_access",
        severity="info",
        actor_type="user",
        actor_id=user.id,
        tenant_id=getattr(agent, "tenant_id", None),
        action=action,
        resource_type=decision_resource_kind,
        resource_id=decision_resource_id,
        details={
            "agent_id": str(agent.id),
            "owner_user_id": str(owner_user_id) if owner_user_id else None,
            "root_session_id": str(root_session_id) if root_session_id else None,
            "actor_role": str(getattr(user, "role", "") or ""),
            "authority_source": SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE,
            "operator_view_requested": operator_view_requested,
            "outcome": "allowed",
            **(details_extra or {}),
        },
    )


async def authorize_resource_action(
    db: AsyncSession,
    user: User,
    *,
    agent_id: uuid.UUID,
    resource_kind: str,
    resource_id: uuid.UUID,
    action: str,
    owner_user_id: uuid.UUID | None = None,
    root_session_id: uuid.UUID | None = None,
    authority_state: str = OWNED_AUTHORITY_STATE,
    allow_manager_override: bool = False,
    manager_override_reason: str | None = None,
    agent_access: tuple[Agent, str] | None = None,
    audit: bool = True,
) -> ResourceAuthorityDecision:
    """Authorize a resource without widening generic Agent access.

    Unknown legacy rows are quarantined before owner/session/grant evaluation.
    Scoped business administrators (PDEC-013) hold business authority over the
    managed company's resources by role, before — and regardless of — any
    optional operator-view projection, mirroring the session lane: an
    administrator who passes operator-view inputs keeps business access with
    the actual scoped administrator authority identified in evidence, never an
    unexplained 403 or a grant requirement. For everyone else, cross-owner
    inspection is read-only and requires an independent live
    ``operator.inspect`` grant plus a reason.
    """

    agent, access_level = agent_access or await (
        check_agent_operator_reachability(db, user, agent_id)
        if allow_manager_override and action == "read"
        else check_agent_access(db, user, agent_id)
    )
    if str(agent.id) != str(agent_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent authority context mismatch")
    state = str(authority_state or QUARANTINED_AUTHORITY_STATE).strip().lower()

    if state == OWNED_AUTHORITY_STATE:
        if owner_user_id is not None and str(owner_user_id) == str(user.id):
            return ResourceAuthorityDecision(
                agent=agent,
                access_level=access_level,
                resource_kind=resource_kind,
                resource_id=resource_id,
                action=action,
                authority_source="resource_owner",
            )
        if await _root_session_owned_by_user(
            db,
            root_session_id=root_session_id,
            agent_id=agent_id,
            user_id=user.id,
        ):
            return ResourceAuthorityDecision(
                agent=agent,
                access_level=access_level,
                resource_kind=resource_kind,
                resource_id=resource_id,
                action=action,
                authority_source="root_session_owner",
            )

    # Scoped business administrators (PDEC-013) manage every business resource
    # of their company by role, before — and regardless of — any ordinary
    # per-resource grant: a cross-owner administrator who also holds a legacy
    # grant is still attributed and audited exactly once as the scoped
    # administrator. The decision stays attributed to the real administrator;
    # callers keep the original manifest owner/root provenance unchanged.
    # Administrator authority takes precedence over the optional operator
    # projection (the same order as the session lane), so an explicitly
    # requested operator view is recorded as ignored evidence instead of
    # turning business access into a 403 or a grant requirement.
    if is_scoped_business_admin(user, resource_tenant_id=getattr(agent, "tenant_id", None)):
        if audit:
            await _audit_scoped_business_admin_resource_access(
                db,
                user=user,
                agent=agent,
                decision_resource_kind=resource_kind,
                decision_resource_id=resource_id,
                action=action,
                owner_user_id=owner_user_id,
                root_session_id=root_session_id,
                operator_view_requested=bool(allow_manager_override),
            )
        return ResourceAuthorityDecision(
            agent=agent,
            access_level=access_level,
            resource_kind=resource_kind,
            resource_id=resource_id,
            action=action,
            authority_source=SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE,
        )

    if state == OWNED_AUTHORITY_STATE and await _has_explicit_resource_grant(
        db,
        user=user,
        resource_kind=resource_kind,
        resource_id=resource_id,
        action=action,
    ):
        return ResourceAuthorityDecision(
            agent=agent,
            access_level=access_level,
            resource_kind=resource_kind,
            resource_id=resource_id,
            action=action,
            authority_source="resource_grant",
        )

    if allow_manager_override and action == "read":
        authority_source = await authorize_agent_operator_inspection(
            db,
            user=user,
            agent=agent,
            reason=manager_override_reason,
            action=f"{resource_kind}:read",
            resource_type=resource_kind,
            resource_id=resource_id,
            details={
                "owner_user_id": str(owner_user_id) if owner_user_id else None,
                "root_session_id": str(root_session_id) if root_session_id else None,
                "authority_state": state,
            },
        )
        return ResourceAuthorityDecision(
            agent=agent,
            access_level=access_level,
            resource_kind=resource_kind,
            resource_id=resource_id,
            action=action,
            authority_source=authority_source,
            operator_view=True,
        )

    detail = (
        "This legacy resource is quarantined and requires an explicit manager operator view"
        if state != OWNED_AUTHORITY_STATE
        else "This resource belongs to a different principal"
    )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


async def filter_authorized_resources(
    db: AsyncSession,
    user: User,
    *,
    agent_id: uuid.UUID,
    resource_kind: str,
    action: str,
    resources: Iterable[Any],
    resource_id_of: Callable[[Any], uuid.UUID] = lambda row: row.id,
    owner_user_id_of: Callable[[Any], uuid.UUID | None] = lambda row: getattr(row, "owner_user_id", None),
    root_session_id_of: Callable[[Any], uuid.UUID | None] = lambda row: getattr(row, "root_session_id", None),
    authority_state_of: Callable[[Any], str] = lambda row: (
        getattr(row, "authority_state", None) or QUARANTINED_AUTHORITY_STATE
    ),
    operator_view: bool = False,
    operator_reason: str | None = None,
    agent_access: tuple[Agent, str] | None = None,
) -> list[tuple[Any, ResourceAuthorityDecision]]:
    """Apply the same row authority contract to list/aggregate surfaces."""

    rows = list(resources)
    resolved_agent_access = agent_access or await (
        check_agent_operator_reachability(db, user, agent_id)
        if operator_view and action == "read"
        else check_agent_access(db, user, agent_id)
    )
    if operator_view:
        collection_id = uuid.uuid5(
            _WORKSPACE_RESOURCE_NAMESPACE,
            f"{agent_id}:{resource_kind}:collection",
        )
        decision = await authorize_resource_action(
            db,
            user,
            agent_id=agent_id,
            resource_kind=f"{resource_kind}_collection",
            resource_id=collection_id,
            action=action,
            authority_state=QUARANTINED_AUTHORITY_STATE,
            allow_manager_override=True,
            manager_override_reason=operator_reason,
            agent_access=resolved_agent_access,
        )
        return [(row, decision) for row in rows]

    visible: list[tuple[Any, ResourceAuthorityDecision]] = []
    for row in rows:
        try:
            decision = await authorize_resource_action(
                db,
                user,
                agent_id=agent_id,
                resource_kind=resource_kind,
                resource_id=resource_id_of(row),
                action=action,
                owner_user_id=owner_user_id_of(row),
                root_session_id=root_session_id_of(row),
                authority_state=authority_state_of(row),
                agent_access=resolved_agent_access,
                # One audit per collection access, not one per visible row.
                audit=False,
            )
        except HTTPException:
            continue
        visible.append((row, decision))

    admin_visible = [
        (row, decision)
        for row, decision in visible
        if decision.authority_source == SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE
    ]
    if admin_visible:
        agent, _access_level = resolved_agent_access
        collection_id = uuid.uuid5(
            _WORKSPACE_RESOURCE_NAMESPACE,
            f"{agent_id}:{resource_kind}:collection",
        )
        # The collection audit names the whole cross-owner target set instead
        # of borrowing provenance from an arbitrary first row.
        distinct_owners = sorted(
            {
                str(owner_id)
                for owner_id in (owner_user_id_of(row) for row, _decision in admin_visible)
                if owner_id is not None
            }
        )
        single_owner = uuid.UUID(distinct_owners[0]) if len(distinct_owners) == 1 else None
        await _audit_scoped_business_admin_resource_access(
            db,
            user=user,
            agent=agent,
            decision_resource_kind=f"{resource_kind}_collection",
            decision_resource_id=collection_id,
            action=action,
            owner_user_id=single_owner,
            root_session_id=None,
            operator_view_requested=False,
            details_extra={
                "target_owner_user_ids": distinct_owners[:50],
                "target_owner_count": len(distinct_owners),
                "target_resource_count": len(admin_visible),
            },
        )
    return visible
