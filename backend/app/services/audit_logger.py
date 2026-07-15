"""Helper to write audit log entries from background services."""

import json
import uuid
from datetime import datetime, timezone

from loguru import logger

from sqlalchemy import text

from app.database import async_session, enter_rls_bypass, tenant_scoped_session


async def _insert_audit_row(
    db,
    *,
    action: str,
    details: dict | None,
    agent_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO audit_logs (id, action, details, agent_id, user_id, tenant_id, created_at) "
            "VALUES (:id, :action, :details, :agent_id, :user_id, :tenant_id, :created_at)"
        ),
        {
            "id": event_id,
            "action": action,
            "details": json.dumps(details or {}, ensure_ascii=False, default=str),
            "agent_id": agent_id,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "created_at": datetime.now(timezone.utc),
        },
    )
    return event_id


async def write_platform_security_audit_event(
    *,
    event_type: str,
    severity: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    action: str,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    details: dict | None = None,
    ip_address: str | None = None,
    request_id: uuid.UUID | None = None,
    execution_identity_type: str | None = None,
    execution_identity_id: uuid.UUID | None = None,
    execution_identity_label: str | None = None,
) -> uuid.UUID:
    """Persist a tenantless security event in the operator-only audit plane.

    Actor identifiers stay in the immutable envelope instead of tenant-bound
    foreign-key columns. This keeps the independent platform audit commit from
    depending on an uncommitted public-registration/OIDC transaction.
    """

    execution_identity = None
    if execution_identity_type or execution_identity_id or execution_identity_label:
        execution_identity = {
            "type": execution_identity_type,
            "id": str(execution_identity_id) if execution_identity_id is not None else None,
            "label": execution_identity_label,
        }
    envelope = {
        "schema_version": "hive.platform_security_audit.v1",
        "event_type": event_type,
        "severity": severity,
        "actor": {
            "type": actor_type,
            "id": str(actor_id) if actor_id is not None else None,
        },
        "action": action,
        "resource": {
            "type": resource_type,
            "id": str(resource_id) if resource_id is not None else None,
        },
        "details": details or {},
        "ip_address": str(ip_address) if ip_address else None,
        "request_id": str(request_id) if request_id is not None else None,
        "execution_identity": execution_identity,
    }
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="operator platform security audit insert") as bypass_db,
    ):
        event_id = await _insert_audit_row(
            bypass_db,
            action=f"platform_security.{event_type}",
            details=envelope,
            agent_id=None,
            user_id=None,
            tenant_id=None,
        )
        await bypass_db.commit()
    return event_id


async def write_audit_log(
    action: str,
    details: dict | None = None,
    agent_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> None:
    """Write a single audit log entry using raw SQL.

    Uses raw SQL to avoid ORM foreign-key resolution issues when
    called from background tasks where not all models may be loaded.

    Args:
        action: Short action string, e.g. "trigger_fire", "schedule_execute".
        details: JSON-serialisable dict with extra info.
        agent_id: Optional agent UUID.
        user_id: Optional user UUID.
    """
    try:
        # RLS stage-2b: audit_logs is now policied (USING-only). Derive tenant_id
        # from the agent so the row is tenant-scoped after the non-owner role flip;
        # a NULL (system-level event, agent_id=None) stays operator-only-by-intent.
        tenant_id = None
        if agent_id is not None:
            from app.services.tenant_resolver import resolve_tenant_for_agent

            tenant_id = await resolve_tenant_for_agent(agent_id)
        if tenant_id is None:
            # Operator/system events intentionally have no tenant.  Their only
            # legal writer is this parameterized audit sink under an explicit
            # BYPASS; a fail-closed request scope cannot and must not create
            # them directly.
            async with (
                async_session() as db,
                enter_rls_bypass(db, reason="operator system audit log insert") as bypass_db,
            ):
                await _insert_audit_row(
                    bypass_db,
                    action=action,
                    details=details,
                    agent_id=agent_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
                await bypass_db.commit()
        else:
            async with tenant_scoped_session(tenant_id, session_factory=async_session) as db:
                await _insert_audit_row(
                    db,
                    action=action,
                    details=details,
                    agent_id=agent_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
                await db.commit()
    except Exception as e:
        # Never let audit logging break the caller
        logger.error(f"[audit_logger] WARNING: failed to write audit log: {e}")
