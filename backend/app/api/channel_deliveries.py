"""Company-operator surface for terminal channel delivery evidence and recovery."""

from __future__ import annotations

from datetime import datetime
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.user import User
from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService

router = APIRouter(prefix="/channel-deliveries", tags=["channel-deliveries"])


class ChannelDeliveryResendRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class ChannelDeliveryItem(BaseModel):
    id: uuid.UUID
    runtime_task_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID
    channel: str
    target: dict[str, Any]
    terminal_status: str
    delivery_kind: str
    status: str
    attempt_count: int
    artifact_count: int
    delivered_part_count: int
    last_error: str | None
    available_at: datetime
    delivered_at: datetime | None
    created_at: datetime


def _require_delivery_operator(user: User) -> User:
    if getattr(user, "role", None) not in {"org_admin", "platform_admin"}:
        raise HTTPException(status_code=403, detail="Company administrator access required")
    return user


def _recipient_hint(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if len(normalized) <= 6:
        return "***"
    return f"{normalized[:3]}…{normalized[-3:]}"


def _redacted_delivery_target(target: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(target or {})
    recipient = next(
        (
            source.get(key)
            for key in (
                "sender_staff_id",
                "sender_id",
                "to_user_id",
                "user_id",
                "open_id",
                "receive_id",
                "chat_id",
                "channel_id",
                "recipient_id",
            )
            if source.get(key)
        ),
        None,
    )
    result = {"channel": str(source.get("channel") or "unknown")}
    hint = _recipient_hint(recipient)
    if hint:
        result["recipient_hint"] = hint
    return result


async def _operator_tenant_id(
    *,
    db: AsyncSession,
    user: User,
    requested_tenant_id: uuid.UUID | None,
) -> uuid.UUID:
    """Resolve the delivery-console tenant scope through the one shared policy.

    An explicit ``tenant_id`` query parameter is only a consistency echo of
    the authenticated selected company (``resolve_and_pin_tenant_scope``); a
    platform administrator passing a foreign or retired company id gets the
    truthful company-selection recovery error instead of a second,
    unvalidated cross-company switch. Organization administrators stay
    bounded to their own company.
    """

    from app.core.tenant_scope import resolve_and_pin_tenant_scope

    _require_delivery_operator(user)
    return await resolve_and_pin_tenant_scope(db, user, requested_tenant_id)


def _serialize_delivery(row: ChannelDeliveryOutbox) -> ChannelDeliveryItem:
    receipts = dict(row.delivery_receipts_json or {})
    delivered_parts = int((receipts.get("text") or {}).get("state") == "delivered")
    delivered_parts += sum(
        1
        for value in (receipts.get("artifacts") or {}).values()
        if isinstance(value, dict) and value.get("state") == "delivered"
    )
    return ChannelDeliveryItem(
        id=row.id,
        runtime_task_id=row.runtime_task_id,
        agent_id=row.agent_id,
        session_id=row.session_id,
        channel=row.channel,
        target=_redacted_delivery_target(row.delivery_target_json),
        terminal_status=row.terminal_status,
        delivery_kind=row.delivery_kind,
        status=row.status,
        attempt_count=int(row.attempt_count or 0),
        artifact_count=len(row.artifact_ids_json or []),
        delivered_part_count=delivered_parts,
        last_error=row.last_error,
        available_at=row.available_at,
        delivered_at=row.delivered_at,
        created_at=row.created_at,
    )


@router.get("", response_model=list[ChannelDeliveryItem])
async def list_channel_deliveries(
    status: str | None = Query(
        default=None, pattern="^(pending|processing|delivered|dead_letter|needs_reconciliation)$"
    ),
    tenant_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChannelDeliveryItem]:
    effective_tenant = await _operator_tenant_id(
        db=db,
        user=current_user,
        requested_tenant_id=tenant_id,
    )
    statement = (
        select(ChannelDeliveryOutbox)
        .where(ChannelDeliveryOutbox.tenant_id == effective_tenant)
        .order_by(ChannelDeliveryOutbox.created_at.desc())
        .limit(limit)
    )
    if status:
        statement = statement.where(ChannelDeliveryOutbox.status == status)
    rows = list((await db.execute(statement)).scalars().all())
    return [_serialize_delivery(row) for row in rows]


@router.post("/{outbox_id}/resend", response_model=ChannelDeliveryItem)
async def resend_channel_delivery(
    outbox_id: uuid.UUID,
    body: ChannelDeliveryResendRequest,
    tenant_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChannelDeliveryItem:
    effective_tenant = await _operator_tenant_id(
        db=db,
        user=current_user,
        requested_tenant_id=tenant_id,
    )
    service = ChannelDeliveryOutboxService()
    try:
        await service.request_manual_resend(
            tenant_id=effective_tenant,
            outbox_id=outbox_id,
            actor_user_id=current_user.id,
            reason=body.reason,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    row = (
        await db.execute(
            select(ChannelDeliveryOutbox).where(
                ChannelDeliveryOutbox.id == outbox_id,
                ChannelDeliveryOutbox.tenant_id == effective_tenant,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="channel delivery outbox item not found")
    return _serialize_delivery(row)
