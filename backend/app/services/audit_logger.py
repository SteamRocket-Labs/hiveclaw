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
) -> None:
    await db.execute(
        text(
            "INSERT INTO audit_logs (id, action, details, agent_id, user_id, tenant_id, created_at) "
            "VALUES (:id, :action, :details, :agent_id, :user_id, :tenant_id, :created_at)"
        ),
        {
            "id": uuid.uuid4(),
            "action": action,
            "details": json.dumps(details or {}, ensure_ascii=False, default=str),
            "agent_id": agent_id,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "created_at": datetime.now(timezone.utc),
        },
    )


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
