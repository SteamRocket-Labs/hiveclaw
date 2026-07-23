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
    event_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    event_id = event_id or uuid.uuid4()
    created_at = created_at or datetime.now(timezone.utc)
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
            "created_at": created_at,
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
    request_id: uuid.UUID | str | None = None,
    execution_identity_type: str | None = None,
    execution_identity_id: uuid.UUID | None = None,
    execution_identity_label: str | None = None,
) -> uuid.UUID:
    """Persist a chained tenantless event in the operator-only audit plane.

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
    base_envelope = {
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
        from app.services.platform_security_audit import (
            acquire_platform_security_chain_lock,
            compute_legacy_platform_audit_anchor,
            load_legacy_platform_security_rows,
            load_platform_security_chain_head,
            platform_security_chain_position,
            seal_platform_security_envelope,
        )

        await acquire_platform_security_chain_lock(bypass_db)
        legacy_rows = await load_legacy_platform_security_rows(bypass_db)
        legacy_anchor = compute_legacy_platform_audit_anchor(legacy_rows)
        chain_head = await load_platform_security_chain_head(bypass_db)
        if chain_head is None:
            cutover_event_id = uuid.uuid4()
            cutover_created_at = datetime.now(timezone.utc)
            cutover_action = "platform_security.chain_cutover"
            cutover_envelope = seal_platform_security_envelope(
                event_id=cutover_event_id,
                row_action=cutover_action,
                base_envelope={
                    "event_type": "chain_cutover",
                    "severity": "info",
                    "actor": {"type": "system", "id": None},
                    "action": "chain_cutover",
                    "resource": {"type": "platform_security_audit", "id": None},
                    "details": legacy_anchor,
                    "legacy_anchor": legacy_anchor,
                    "ip_address": None,
                    "request_id": None,
                    "execution_identity": None,
                },
                sequence_num=1,
                prev_hash="genesis",
                recorded_at=cutover_created_at,
            )
            await _insert_audit_row(
                bypass_db,
                action=cutover_action,
                details=cutover_envelope,
                agent_id=None,
                user_id=None,
                tenant_id=None,
                event_id=cutover_event_id,
                created_at=cutover_created_at,
            )
            previous_sequence = 1
            previous_hash = cutover_envelope["event_hash"]
        else:
            previous_sequence, previous_hash = platform_security_chain_position(chain_head)

        event_id = uuid.uuid4()
        created_at = datetime.now(timezone.utc)
        row_action = f"platform_security.{event_type}"
        base_envelope["legacy_anchor"] = legacy_anchor
        envelope = seal_platform_security_envelope(
            event_id=event_id,
            row_action=row_action,
            base_envelope=base_envelope,
            sequence_num=previous_sequence + 1,
            prev_hash=previous_hash,
            recorded_at=created_at,
        )
        event_id = await _insert_audit_row(
            bypass_db,
            action=row_action,
            details=envelope,
            agent_id=None,
            user_id=None,
            tenant_id=None,
            event_id=event_id,
            created_at=created_at,
        )
        await bypass_db.commit()
    return event_id


async def write_audit_log(
    action: str,
    details: dict | None = None,
    agent_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> None:
    """Write a best-effort operational audit row using raw SQL.

    Uses raw SQL to avoid ORM foreign-key resolution issues when
    called from background tasks where not all models may be loaded.
    Canonical platform-security evidence is not accepted here because this
    compatibility sink intentionally remains fail-soft.

    Args:
        action: Short action string, e.g. "trigger_fire", "schedule_execute".
        details: JSON-serialisable dict with extra info.
        agent_id: Optional agent UUID.
        user_id: Optional user UUID.
    """
    if action.startswith("platform_security."):
        raise ValueError("platform_security.* events must use write_platform_security_audit_event")
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
