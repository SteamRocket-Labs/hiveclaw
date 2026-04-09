"""Desktop audit event ingestion endpoints (ARCHITECTURE.md §7.5).

POST /desktop/audit/events       — batch tool/operation audit from Desktop
POST /desktop/audit/guard-events — Guard interception events from Desktop
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.audit import AuditLog
from app.models.user import User

router = APIRouter(prefix="/desktop", tags=["desktop-audit"])


# ─── Schemas ────────────────────────────────────────────


class DesktopAuditEvent(BaseModel):
    action: str
    agent_id: uuid.UUID | None = None
    details: dict = {}
    timestamp: datetime | None = None


class DesktopGuardEvent(BaseModel):
    action: str
    agent_id: uuid.UUID | None = None
    rule: str = ""
    blocked: bool = True
    details: dict = {}
    timestamp: datetime | None = None


class AuditBatchRequest(BaseModel):
    events: list[DesktopAuditEvent] = Field(max_length=500)


class GuardEventBatchRequest(BaseModel):
    events: list[DesktopGuardEvent] = Field(max_length=500)


class AuditBatchResponse(BaseModel):
    accepted: int


# ─── Endpoints ──────────────────────────────────────────


@router.post("/audit/events", response_model=AuditBatchResponse, status_code=status.HTTP_201_CREATED)
async def ingest_audit_events(
    body: AuditBatchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Receive a batch of tool/operation audit events from Desktop."""
    for event in body.events:
        db.add(AuditLog(
            user_id=current_user.id,
            agent_id=event.agent_id,
            action=f"desktop:{event.action}",
            details={**event.details, "source": "desktop"},
        ))
    await db.flush()
    return AuditBatchResponse(accepted=len(body.events))


@router.post("/audit/guard-events", response_model=AuditBatchResponse, status_code=status.HTTP_201_CREATED)
async def ingest_guard_events(
    body: GuardEventBatchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Receive Guard interception events from Desktop."""
    for event in body.events:
        db.add(AuditLog(
            user_id=current_user.id,
            agent_id=event.agent_id,
            action=f"desktop:guard:{event.action}",
            details={
                "rule": event.rule,
                "blocked": event.blocked,
                **event.details,
                "source": "desktop",
            },
        ))
    await db.flush()
    return AuditBatchResponse(accepted=len(body.events))
