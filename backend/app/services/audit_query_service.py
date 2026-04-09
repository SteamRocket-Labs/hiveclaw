"""Unified audit query service for SecurityAuditEvent table.

Provides filtered queries, CSV export, and hash-chain verification.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.security_audit import SecurityAuditEvent
from app.schemas.audit_schemas import AuditQueryParams

logger = logging.getLogger(__name__)


def _apply_filters(
    query,
    tenant_id: uuid.UUID,
    params: AuditQueryParams,
):
    """Apply WHERE clauses from AuditQueryParams to a query."""
    query = query.where(SecurityAuditEvent.tenant_id == tenant_id)

    if params.event_type:
        query = query.where(SecurityAuditEvent.event_type == params.event_type)
    if params.severity:
        query = query.where(SecurityAuditEvent.severity == params.severity)
    if params.actor_id:
        query = query.where(SecurityAuditEvent.actor_id == params.actor_id)
    if params.resource_type:
        query = query.where(SecurityAuditEvent.resource_type == params.resource_type)
    if params.resource_id:
        query = query.where(SecurityAuditEvent.resource_id == params.resource_id)
    if params.date_from:
        query = query.where(SecurityAuditEvent.created_at >= params.date_from)
    if params.date_to:
        query = query.where(SecurityAuditEvent.created_at <= params.date_to)
    if params.search:
        pattern = f"%{params.search}%"
        query = query.where(
            SecurityAuditEvent.action.ilike(pattern)
            | SecurityAuditEvent.event_type.ilike(pattern)
            | SecurityAuditEvent.details.cast(str).ilike(pattern)
        )

    return query


async def query_events(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    params: AuditQueryParams,
) -> tuple[list[SecurityAuditEvent], int]:
    """Query security audit events with filtering and pagination.

    Returns (events, total_count).
    """
    base = select(SecurityAuditEvent)
    base = _apply_filters(base, tenant_id, params)

    # Total count
    count_query = select(func.count()).select_from(base.order_by(None).subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Paginated results
    offset = (params.page - 1) * params.page_size
    data_query = base.order_by(SecurityAuditEvent.created_at.desc()).offset(offset).limit(params.page_size)
    result = await db.execute(data_query)
    events = list(result.scalars().all())

    return events, total


CSV_EXPORT_MAX_ROWS = 50000


async def export_csv(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    params: AuditQueryParams,
) -> str:
    """Export filtered audit events as a CSV string (capped at CSV_EXPORT_MAX_ROWS to prevent OOM)."""
    base = select(SecurityAuditEvent)
    base = _apply_filters(base, tenant_id, params)
    base = base.order_by(SecurityAuditEvent.created_at.desc()).limit(CSV_EXPORT_MAX_ROWS)

    result = await db.execute(base)
    events = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "timestamp",
            "event_type",
            "severity",
            "actor_type",
            "actor_id",
            "action",
            "resource_type",
            "resource_id",
            "ip_address",
            "details",
        ]
    )
    for evt in events:
        writer.writerow(
            [
                evt.created_at.strftime("%Y-%m-%d %H:%M:%S") if evt.created_at else "",
                evt.event_type,
                evt.severity,
                evt.actor_type,
                str(evt.actor_id) if evt.actor_id else "",
                evt.action,
                evt.resource_type or "",
                str(evt.resource_id) if evt.resource_id else "",
                evt.ip_address or "",
                json.dumps(evt.details, ensure_ascii=False) if evt.details else "",
            ]
        )

    return output.getvalue()


async def verify_chain(
    db: AsyncSession,
    event_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> dict:
    """Verify hash-chain integrity for a single audit event.

    Recomputes the hash from the event's fields + prev_hash and checks
    it matches the stored event_hash. Also locates the predecessor event.
    Tenant-scoped to prevent cross-tenant information disclosure.

    Returns {valid, event_hash, computed_hash, predecessor_id}.
    """
    result = await db.execute(
        select(SecurityAuditEvent).where(
            SecurityAuditEvent.id == event_id,
            SecurityAuditEvent.tenant_id == tenant_id,
        )
    )
    event = result.scalar_one_or_none()
    if not event:
        return {"valid": False, "event_hash": "", "computed_hash": "", "predecessor_id": None}

    # Recompute hash using the same algorithm as write_audit_event in policy.py
    hash_input = json.dumps(
        {
            "event_type": event.event_type,
            "actor_type": event.actor_type,
            "actor_id": str(event.actor_id),
            "tenant_id": str(event.tenant_id),
            "action": event.action,
            "prev_hash": event.prev_hash,
        },
        sort_keys=True,
    )
    computed_hash = hashlib.sha256(hash_input.encode()).hexdigest()

    # Find predecessor by matching prev_hash
    predecessor_id: uuid.UUID | None = None
    if event.prev_hash and event.prev_hash != "genesis":
        pred_result = await db.execute(
            select(SecurityAuditEvent.id).where(SecurityAuditEvent.event_hash == event.prev_hash).limit(1)
        )
        predecessor_id = pred_result.scalar_one_or_none()

    return {
        "valid": computed_hash == event.event_hash,
        "event_hash": event.event_hash,
        "computed_hash": computed_hash,
        "predecessor_id": predecessor_id,
    }
