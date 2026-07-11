"""Enterprise AI asset catalog, revision, rollback, and reconciliation API."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services import ai_assets as ai_asset_service


router = APIRouter(prefix="/enterprise/ai-assets", tags=["enterprise-ai-assets"])


class RollbackAssetRequest(BaseModel):
    target_version: int = Field(ge=1)


def _tenant_id(user: User) -> uuid.UUID:
    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A selected tenant is required")
    return tenant_id


def _require_admin(user: User) -> None:
    if getattr(user, "role", None) not in {"org_admin", "platform_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AI asset mutation requires admin access")


@router.get("")
async def list_ai_assets(
    asset_type: str | None = Query(default=None),
    lifecycle_status: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await ai_asset_service.list_assets(
        db,
        tenant_id=_tenant_id(current_user),
        asset_type=asset_type,
        lifecycle_status=lifecycle_status,
    )


@router.get("/{asset_id}")
async def get_ai_asset(
    asset_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant_id = _tenant_id(current_user)
    record = await ai_asset_service.get_asset_record(db, tenant_id=tenant_id, asset_id=asset_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI asset not found")
    history = await ai_asset_service.revision_history(db, tenant_id=tenant_id, asset_id=asset_id, limit=20)
    usage_events = await ai_asset_service.list_asset_usage_events(
        db,
        tenant_id=tenant_id,
        asset_id=asset_id,
        limit=100,
    )
    active = history[0] if history else None
    return {
        "asset": ai_asset_service.asset_payload(record),
        "active_revision": active,
        "history": history,
        "usage_events": usage_events,
    }


@router.get("/{asset_id}/revisions")
async def list_ai_asset_revisions(
    asset_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    try:
        return await ai_asset_service.revision_history(
            db,
            tenant_id=_tenant_id(current_user),
            asset_id=asset_id,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{asset_id}/revisions/{version}")
async def get_ai_asset_revision(
    asset_id: uuid.UUID,
    version: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        revision = await ai_asset_service.revision_detail(
            db,
            tenant_id=_tenant_id(current_user),
            asset_id=asset_id,
            version=version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI asset revision not found")
    return revision


@router.post("/{asset_id}/rollback")
async def rollback_ai_asset(
    asset_id: uuid.UUID,
    body: RollbackAssetRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_admin(current_user)
    try:
        record, revision = await ai_asset_service.rollback_asset(
            db,
            tenant_id=_tenant_id(current_user),
            asset_id=asset_id,
            target_version=body.target_version,
            actor_user_id=current_user.id,
        )
    except ValueError as exc:
        await db.rollback()
        await ai_asset_service.record_projection_failure(
            db,
            tenant_id=_tenant_id(current_user),
            asset_id=asset_id,
            operation="rollback",
            error=exc,
            actor_user_id=current_user.id,
        )
        await db.commit()
        code = status.HTTP_404_NOT_FOUND if "not found" in str(exc).lower() else status.HTTP_409_CONFLICT
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    except Exception as exc:
        await db.rollback()
        await ai_asset_service.record_projection_failure(
            db,
            tenant_id=_tenant_id(current_user),
            asset_id=asset_id,
            operation="rollback",
            error=exc,
            actor_user_id=current_user.id,
        )
        await db.commit()
        raise
    return {"asset_id": str(record.id), "revision_id": str(revision.id), "version": revision.version}


@router.post("/{asset_id}/reconcile")
async def reconcile_ai_asset(
    asset_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_admin(current_user)
    try:
        result = await ai_asset_service.reconcile_asset(
            db,
            tenant_id=_tenant_id(current_user),
            asset_id=asset_id,
        )
        await db.commit()
        return result
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
