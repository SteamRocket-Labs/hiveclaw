"""Tenant-operator inspection and recovery of committed child-result delivery."""

from datetime import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.channel_deliveries import _operator_tenant_id
from app.core.security import get_current_user
from app.database import get_db
from app.models.runtime_result import RuntimeResultIntegrationPage
from app.models.user import User
from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

router = APIRouter(prefix="/runtime-result-pages", tags=["runtime-result-pages"])


class RuntimeResultPageItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    parent_session_id: uuid.UUID
    parent_agent_id: uuid.UUID
    integration_epoch: int
    delivery_mode: str
    item_count: int
    manifest_sha256: str
    status: str
    attempt_count: int
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ResultPageRedriveRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


@router.get("", response_model=list[RuntimeResultPageItem])
async def list_runtime_result_pages(
    parent_session_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(prepared|processing|delivered|dead_letter)$"),
    tenant_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RuntimeResultPageItem]:
    effective_tenant = await _operator_tenant_id(db=db, user=current_user, requested_tenant_id=tenant_id)
    statement = (
        select(RuntimeResultIntegrationPage)
        .where(
            RuntimeResultIntegrationPage.tenant_id == effective_tenant,
        )
        .order_by(RuntimeResultIntegrationPage.created_at.desc())
        .limit(limit)
    )
    if parent_session_id:
        statement = statement.where(RuntimeResultIntegrationPage.parent_session_id == parent_session_id)
    if status:
        statement = statement.where(RuntimeResultIntegrationPage.status == status)
    rows = (await db.execute(statement)).scalars().all()
    return [RuntimeResultPageItem.model_validate(row) for row in rows]


@router.post("/{page_id}/redrive", response_model=RuntimeResultPageItem)
async def redrive_runtime_result_page(
    page_id: uuid.UUID,
    body: ResultPageRedriveRequest,
    tenant_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeResultPageItem:
    effective_tenant = await _operator_tenant_id(db=db, user=current_user, requested_tenant_id=tenant_id)
    try:
        row = await RuntimeNotificationOutboxService().redrive_dead_letter_page(
            tenant_id=effective_tenant,
            page_id=page_id,
            actor_user_id=current_user.id,
            reason=body.reason,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RuntimeResultPageItem.model_validate(row)
