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

from app.core.permissions import check_agent_access
from app.core.policy import check_permission, permission_allows
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
    for principal_type, principal_id in principals:
        if await check_permission(
            db,
            principal_type=principal_type,
            principal_id=principal_id,
            resource_type=resource_kind,
            resource_id=resource_id,
            action=action,
            context=context,
        ):
            return True
    return False


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
    return {
        permission.resource_id
        for permission in permissions
        if permission_allows(permission, action=action, context=context)
    }


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
) -> ResourceAuthorityDecision:
    """Authorize a resource without widening generic Agent access.

    Unknown legacy rows are quarantined before owner/session/grant evaluation.
    A manager can cross either an ownership or quarantine boundary only when a
    caller deliberately enables operator mode and supplies a reason.
    """

    agent, access_level = agent_access or await check_agent_access(db, user, agent_id)
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
        if await _has_explicit_resource_grant(
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

    reason = str(manager_override_reason or "").strip()
    if access_level == "manage" and allow_manager_override and reason:
        from app.services.audit_logger import write_audit_log

        await write_audit_log(
            "resource_authority_override",
            details={
                "resource_kind": resource_kind,
                "resource_id": str(resource_id),
                "owner_user_id": str(owner_user_id) if owner_user_id else None,
                "root_session_id": str(root_session_id) if root_session_id else None,
                "authority_state": state,
                "action": action,
                "reason": reason,
                "authority_source": "manager_override",
            },
            agent_id=agent_id,
            user_id=user.id,
        )
        return ResourceAuthorityDecision(
            agent=agent,
            access_level=access_level,
            resource_kind=resource_kind,
            resource_id=resource_id,
            action=action,
            authority_source="manager_override",
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
    resolved_agent_access = agent_access or await check_agent_access(db, user, agent_id)
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
            )
        except HTTPException:
            continue
        visible.append((row, decision))
    return visible
