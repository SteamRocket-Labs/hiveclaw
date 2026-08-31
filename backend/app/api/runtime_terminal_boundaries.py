"""Company-operator surface for terminal-boundary recovery."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.channel_deliveries import _operator_tenant_id
from app.core.security import get_current_user
from app.database import get_db
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.models.user import User
from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

router = APIRouter(prefix="/runtime-terminal-boundaries", tags=["runtime-terminal-boundaries"])


class RuntimeTerminalBoundaryRedriveRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    summary_disposition: Literal["retry"] | None = None


class RuntimeTerminalBoundaryItem(BaseModel):
    id: uuid.UUID
    runtime_task_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: str
    event_kind: str
    terminal_status: str
    authority_ref: str
    authority_id: str
    status: str
    attempt_count: int
    last_error: str | None
    available_at: datetime
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _serialize_terminal_boundary(row: RuntimeTerminalBoundaryOutbox) -> RuntimeTerminalBoundaryItem:
    return RuntimeTerminalBoundaryItem(
        id=row.id,
        runtime_task_id=row.runtime_task_id,
        agent_id=row.agent_id,
        session_id=row.session_id,
        event_kind=row.event_kind,
        terminal_status=row.terminal_status,
        authority_ref=row.authority_ref,
        authority_id=row.authority_id,
        status=row.status,
        attempt_count=int(row.attempt_count or 0),
        last_error=row.last_error,
        available_at=row.available_at,
        delivered_at=row.delivered_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[RuntimeTerminalBoundaryItem])
async def list_runtime_terminal_boundaries(
    status: str | None = Query(default=None, pattern="^(pending|processing|delivered|dead_letter)$"),
    tenant_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RuntimeTerminalBoundaryItem]:
    effective_tenant = await _operator_tenant_id(
        db=db,
        user=current_user,
        requested_tenant_id=tenant_id,
    )
    statement = (
        select(RuntimeTerminalBoundaryOutbox)
        .where(RuntimeTerminalBoundaryOutbox.tenant_id == effective_tenant)
        .order_by(RuntimeTerminalBoundaryOutbox.created_at.desc())
        .limit(limit)
    )
    if status:
        statement = statement.where(RuntimeTerminalBoundaryOutbox.status == status)
    rows = list((await db.execute(statement)).scalars().all())
    return [_serialize_terminal_boundary(row) for row in rows]


@router.post("/{outbox_id}/redrive", response_model=RuntimeTerminalBoundaryItem)
async def redrive_runtime_terminal_boundary(
    outbox_id: uuid.UUID,
    body: RuntimeTerminalBoundaryRedriveRequest,
    tenant_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeTerminalBoundaryItem:
    effective_tenant = await _operator_tenant_id(
        db=db,
        user=current_user,
        requested_tenant_id=tenant_id,
    )
    try:
        row = await RuntimeTerminalBoundaryOutboxService().redrive_dead_letter(
            tenant_id=effective_tenant,
            outbox_id=outbox_id,
            actor_user_id=current_user.id,
            reason=body.reason,
            summary_disposition=body.summary_disposition,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize_terminal_boundary(row)
