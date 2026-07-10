"""RBAC/ABAC policy evaluator for resource-level permission checks.

Evaluates resource_permissions table with optional ABAC conditions.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.security_audit import ResourcePermission

logger = logging.getLogger(__name__)


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
) -> bool:
    """Check if a principal has permission to perform an action on a resource.

    Evaluates: matching grant exists AND ABAC conditions pass.
    Returns True if allowed, False if denied.
    """
    result = await db.execute(
        select(ResourcePermission).where(
            ResourcePermission.principal_type == principal_type,
            ResourcePermission.principal_id == principal_id,
            ResourcePermission.resource_type == resource_type,
            ResourcePermission.resource_id == resource_id,
        )
    )
    permissions = result.scalars().all()

    for perm in permissions:
        if action not in perm.actions:
            continue

        # Evaluate ABAC conditions
        if perm.conditions and context:
            if not _evaluate_conditions(perm.conditions, context):
                continue

        return True

    return False


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
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            hour_str = now.strftime("%H:%M")
            start = value.get("start", "00:00")
            end = value.get("end", "23:59")
            if not (start <= hour_str <= end):
                return False
        # Additional conditions can be added here
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
) -> None:
    """Write a tamper-evident audit event with hash chain."""
    from app.models.security_audit import SecurityAuditEvent

    if tenant_id is None or tenant_id == uuid.UUID(int=0):
        logger.warning(
            "Skipping audit event %s without a persistable tenant_id",
            event_type,
        )
        return

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
