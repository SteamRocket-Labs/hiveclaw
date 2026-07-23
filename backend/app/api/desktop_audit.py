"""Desktop audit event ingestion endpoints (ARCHITECTURE.md §7.5).

POST /desktop/audit/events       — batch tool/operation audit from Desktop
POST /desktop/audit/guard-events — Guard interception events from Desktop
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.user import User

router = APIRouter(prefix="/desktop", tags=["desktop-audit"])


# ─── Schemas ────────────────────────────────────────────


class DesktopAuditEvent(BaseModel):
    action: str = Field(min_length=1, max_length=92, pattern=r"^[A-Za-z0-9_.:-]+$")
    agent_id: uuid.UUID | None = None
    details: dict = Field(default_factory=dict)
    timestamp: datetime | None = None


class DesktopGuardEvent(BaseModel):
    action: str = Field(min_length=1, max_length=86, pattern=r"^[A-Za-z0-9_.:-]+$")
    agent_id: uuid.UUID | None = None
    rule: str = Field(default="", max_length=200)
    blocked: bool = True
    details: dict = Field(default_factory=dict)
    timestamp: datetime | None = None


class AuditBatchRequest(BaseModel):
    events: list[DesktopAuditEvent] = Field(max_length=500)


class GuardEventBatchRequest(BaseModel):
    events: list[DesktopGuardEvent] = Field(max_length=500)


class AuditBatchResponse(BaseModel):
    accepted: int


# ─── Endpoints ──────────────────────────────────────────


def _require_authenticated_tenant(current_user: User) -> uuid.UUID:
    tenant_id = getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "desktop_audit_tenant_required",
                "message": "Desktop audit ingestion requires an authenticated tenant scope",
            },
        )
    return tenant_id


async def _authorize_claimed_agents(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_ids: set[uuid.UUID],
) -> None:
    if not agent_ids:
        return
    result = await db.execute(
        select(Agent.id).where(
            Agent.id.in_(agent_ids),
            Agent.tenant_id == tenant_id,
        )
    )
    authorized_ids = set(result.scalars().all())
    denied_ids = sorted(str(agent_id) for agent_id in agent_ids - authorized_ids)
    if denied_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "desktop_audit_agent_scope_denied",
                "message": "A claimed agent is outside the authenticated tenant",
                "denied_agent_ids": denied_ids,
            },
        )


def _client_asserted_envelope(
    request: Request,
    *,
    current_user: User,
    agent_id: uuid.UUID | None,
    timestamp: datetime | None,
    claimed_details: dict,
) -> dict:
    trace_id = getattr(request.state, "trace_id", None)
    return {
        "schema_version": "hive.desktop_client_audit.v1",
        "evidence_trust": "client_asserted",
        "source": "desktop",
        "authenticated_user_id": str(current_user.id),
        "authenticated_tenant_id": str(current_user.tenant_id),
        "claimed_agent_id": str(agent_id) if agent_id is not None else None,
        "claimed_timestamp": timestamp.astimezone(timezone.utc).isoformat() if timestamp else None,
        "claimed_details": claimed_details,
        "request_id": str(trace_id) if trace_id else None,
    }


@router.post("/audit/events", response_model=AuditBatchResponse, status_code=status.HTTP_201_CREATED)
async def ingest_audit_events(
    body: AuditBatchRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Receive a batch of tool/operation audit events from Desktop."""
    tenant_id = _require_authenticated_tenant(current_user)
    await _authorize_claimed_agents(
        db,
        tenant_id=tenant_id,
        agent_ids={event.agent_id for event in body.events if event.agent_id is not None},
    )
    for event in body.events:
        db.add(
            AuditLog(
                user_id=current_user.id,
                agent_id=event.agent_id,
                tenant_id=tenant_id,
                action=f"desktop:{event.action}",
                details=_client_asserted_envelope(
                    request,
                    current_user=current_user,
                    agent_id=event.agent_id,
                    timestamp=event.timestamp,
                    claimed_details=event.details,
                ),
            )
        )
    await db.flush()
    return AuditBatchResponse(accepted=len(body.events))


@router.post("/audit/guard-events", response_model=AuditBatchResponse, status_code=status.HTTP_201_CREATED)
async def ingest_guard_events(
    body: GuardEventBatchRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Receive Guard interception events from Desktop."""
    tenant_id = _require_authenticated_tenant(current_user)
    await _authorize_claimed_agents(
        db,
        tenant_id=tenant_id,
        agent_ids={event.agent_id for event in body.events if event.agent_id is not None},
    )
    for event in body.events:
        envelope = _client_asserted_envelope(
            request,
            current_user=current_user,
            agent_id=event.agent_id,
            timestamp=event.timestamp,
            claimed_details=event.details,
        )
        envelope.update(
            {
                "rule": event.rule,
                "blocked": event.blocked,
            }
        )
        db.add(
            AuditLog(
                user_id=current_user.id,
                agent_id=event.agent_id,
                tenant_id=tenant_id,
                action=f"desktop:guard:{event.action}",
                details=envelope,
            )
        )
    await db.flush()
    return AuditBatchResponse(accepted=len(body.events))
