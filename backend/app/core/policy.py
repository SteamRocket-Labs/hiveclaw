"""RBAC/ABAC policy evaluator for resource-level permission checks.

Evaluates resource_permissions table with optional ABAC conditions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import ipaddress
import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.security_audit import ResourcePermission

logger = logging.getLogger(__name__)


class SecurityAuditScope(StrEnum):
    TENANT = "tenant_security"
    PLATFORM = "platform_operator"


@dataclass(frozen=True, slots=True)
class SecurityAuditWriteReceipt:
    event_id: uuid.UUID
    scope: SecurityAuditScope
    tenant_id: uuid.UUID | None


def compute_audit_event_hash(
    *,
    event_type: str,
    severity: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    tenant_id: uuid.UUID,
    action: str,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    details: dict | None = None,
    ip_address: str | None = None,
    request_id: uuid.UUID | None = None,
    execution_identity_type: str | None = None,
    execution_identity_id: uuid.UUID | None = None,
    execution_identity_label: str | None = None,
    prev_hash: str,
) -> str:
    """Compute the canonical tamper-evident hash for an audit event."""
    import hashlib
    import json

    payload = {
        "event_type": event_type,
        "severity": severity,
        "actor_type": actor_type,
        "actor_id": str(actor_id) if actor_id is not None else None,
        "tenant_id": str(tenant_id),
        "action": action,
        "resource_type": resource_type,
        "resource_id": str(resource_id) if resource_id is not None else None,
        "details": details or {},
        "ip_address": str(ip_address) if ip_address else None,
        "request_id": str(request_id) if request_id is not None else None,
        "prev_hash": prev_hash,
    }
    if execution_identity_type or execution_identity_id or execution_identity_label:
        payload["execution_identity"] = {
            "type": execution_identity_type,
            "id": str(execution_identity_id) if execution_identity_id is not None else None,
            "label": execution_identity_label,
        }
    hash_input = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(hash_input.encode()).hexdigest()


def compute_legacy_audit_event_hash(
    *,
    event_type: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    tenant_id: uuid.UUID,
    action: str,
    prev_hash: str,
) -> str:
    """Compute the pre-2026-06 audit hash for historical chain verification."""
    import hashlib
    import json

    hash_input = json.dumps(
        {
            "event_type": event_type,
            "actor_type": actor_type,
            "actor_id": str(actor_id),
            "tenant_id": str(tenant_id),
            "action": action,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
    )
    return hashlib.sha256(hash_input.encode()).hexdigest()


def _audit_chain_lock_key(tenant_id: uuid.UUID) -> int:
    import hashlib

    return int(hashlib.sha256(str(tenant_id).encode("ascii")).hexdigest()[:16], 16) & ((1 << 63) - 1)


def _db_dialect_name(db: AsyncSession) -> str:
    try:
        bind = db.get_bind()
    except Exception:
        bind = getattr(db, "bind", None)
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "")


async def _lock_audit_chain(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    if _db_dialect_name(db) != "postgresql":
        return
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _audit_chain_lock_key(tenant_id)},
    )


async def check_permission(
    db: AsyncSession,
    *,
    principal_type: str,
    principal_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
    action: str,
    context: dict | None = None,
    additional_principals: list[tuple[str, uuid.UUID]] | None = None,
) -> bool:
    """Check if a principal has permission to perform an action on a resource.

    Evaluates live matching allow/deny rows; any applicable deny wins.
    Returns True if allowed, False if denied.
    """
    principals = [(principal_type, principal_id), *(additional_principals or [])]
    statement = select(ResourcePermission).where(
        or_(
            *(
                and_(
                    ResourcePermission.principal_type == candidate_type,
                    ResourcePermission.principal_id == candidate_id,
                )
                for candidate_type, candidate_id in principals
            )
        ),
        ResourcePermission.resource_type == resource_type,
        ResourcePermission.resource_id == resource_id,
    )
    tenant_id = (context or {}).get("tenant_id")
    if tenant_id:
        try:
            statement = statement.where(ResourcePermission.tenant_id == uuid.UUID(str(tenant_id)))
        except (TypeError, ValueError):
            return False
    result = await db.execute(statement)
    permissions = result.scalars().all()

    applicable = [
        permission
        for permission in permissions
        if permission_effect_applies(permission, action=action, context=context)
    ]
    if any(str(getattr(permission, "effect", "allow") or "allow") == "deny" for permission in applicable):
        return False
    return any(str(getattr(permission, "effect", "allow") or "allow") == "allow" for permission in applicable)


def permission_effect_applies(
    permission: ResourcePermission,
    *,
    action: str,
    context: dict | None = None,
    now: datetime | None = None,
) -> bool:
    """Return whether one allow/deny row is live and matches the request."""

    if action not in (getattr(permission, "actions", None) or []):
        return False
    if getattr(permission, "revoked_at", None) is not None:
        return False
    expires_at = getattr(permission, "expires_at", None)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= (now or datetime.now(timezone.utc)):
            return False
    conditions = getattr(permission, "conditions", None)
    if conditions is None:
        conditions = {}
    return isinstance(conditions, dict) and (not conditions or _evaluate_conditions(conditions, context or {}))


def permission_allows(permission: ResourcePermission, *, action: str, context: dict | None = None) -> bool:
    """Evaluate one already-loaded grant without another database round trip."""

    return str(getattr(permission, "effect", "allow") or "allow") == "allow" and permission_effect_applies(
        permission, action=action, context=context
    )


def _evaluate_conditions(conditions: dict, context: dict) -> bool:
    """Evaluate ABAC conditions against request context.

    Supported condition keys:
      - time_range: {"start": "09:00", "end": "18:00"} — business hours
      - environment: "production" | "staging"
      - ip_ranges: ["10.0.0.0/8", "172.16.0.0/12"]
    """
    for key, value in conditions.items():
        if key == "environment":
            if context.get("environment") != value:
                return False
        elif key == "time_range":
            if not isinstance(value, dict):
                return False
            try:
                current = datetime.now(timezone.utc).time().replace(second=0, microsecond=0)
                start = datetime.strptime(str(value.get("start", "00:00")), "%H:%M").time()
                end = datetime.strptime(str(value.get("end", "23:59")), "%H:%M").time()
            except (TypeError, ValueError):
                return False
            inside = start <= current <= end if start <= end else current >= start or current <= end
            if not inside:
                return False
        elif key == "ip_ranges":
            if not isinstance(value, (list, tuple)):
                return False
            try:
                request_ip = ipaddress.ip_address(str(context.get("ip_address") or ""))
                networks = [ipaddress.ip_network(str(item), strict=False) for item in value]
            except (TypeError, ValueError):
                return False
            if not any(request_ip in network for network in networks):
                return False
        else:
            return False
    return True


async def enforce_permission(
    db: AsyncSession,
    *,
    principal_type: str,
    principal_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
    action: str,
    context: dict | None = None,
) -> None:
    """Check permission and raise 403 if denied."""
    allowed = await check_permission(
        db,
        principal_type=principal_type,
        principal_id=principal_id,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        context=context,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {action} on {resource_type}/{resource_id}",
        )


async def write_audit_event(
    db: AsyncSession,
    *,
    event_type: str,
    severity: str = "info",
    actor_type: str,
    actor_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    action: str,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    details: dict | None = None,
    ip_address: str | None = None,
    request_id: uuid.UUID | None = None,
) -> SecurityAuditWriteReceipt:
    """Write to the tenant hash chain or immutable operator audit plane."""
    from app.models.security_audit import SecurityAuditEvent

    # Read execution identity from ContextVar (set by channel handlers / trigger daemon)
    exec_identity_type = None
    exec_identity_id = None
    exec_identity_label = None
    try:
        from app.core.execution_context import get_execution_identity

        identity = get_execution_identity()
        if identity:
            exec_identity_type = identity.identity_type
            exec_identity_id = identity.identity_id
            exec_identity_label = identity.label
    except Exception as exc:  # noqa: BLE001 - audit writing must not fail on optional context capture
        logger.debug("Execution identity unavailable for audit event %s: %s", event_type, exc)

    if tenant_id is None or tenant_id == uuid.UUID(int=0):
        from app.services.audit_logger import write_platform_security_audit_event

        event_id = await write_platform_security_audit_event(
            event_type=event_type,
            severity=severity,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            ip_address=ip_address,
            request_id=request_id,
            execution_identity_type=exec_identity_type,
            execution_identity_id=exec_identity_id,
            execution_identity_label=exec_identity_label,
        )
        return SecurityAuditWriteReceipt(
            event_id=event_id,
            scope=SecurityAuditScope.PLATFORM,
            tenant_id=None,
        )

    await _lock_audit_chain(db, tenant_id)

    # Get previous hash for this tenant's chain. PostgreSQL deployments take a
    # transaction advisory lock above so concurrent writers cannot fork prev_hash.
    result = await db.execute(
        select(SecurityAuditEvent.event_hash)
        .where(SecurityAuditEvent.tenant_id == tenant_id)
        .order_by(
            SecurityAuditEvent.sequence_num.desc().nullslast(),
            SecurityAuditEvent.created_at.desc(),
            SecurityAuditEvent.id.desc(),
        )
        .limit(1)
    )
    prev_hash = result.scalar_one_or_none() or "genesis"

    event_hash = compute_audit_event_hash(
        event_type=event_type,
        severity=severity,
        actor_type=actor_type,
        actor_id=actor_id,
        tenant_id=tenant_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        ip_address=ip_address,
        request_id=request_id,
        execution_identity_type=exec_identity_type,
        execution_identity_id=exec_identity_id,
        execution_identity_label=exec_identity_label,
        prev_hash=prev_hash,
    )

    event = SecurityAuditEvent(
        event_type=event_type,
        severity=severity,
        actor_type=actor_type,
        actor_id=actor_id,
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        details=details or {},
        ip_address=ip_address,
        request_id=request_id,
        prev_hash=prev_hash,
        event_hash=event_hash,
        execution_identity_type=exec_identity_type,
        execution_identity_id=exec_identity_id,
        execution_identity_label=exec_identity_label,
    )
    db.add(event)
    await db.flush()
    return SecurityAuditWriteReceipt(
        event_id=event.id,
        scope=SecurityAuditScope.TENANT,
        tenant_id=tenant_id,
    )
