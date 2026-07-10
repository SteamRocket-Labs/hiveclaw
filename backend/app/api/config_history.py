"""Compatibility adapter for the governed AI-asset revision API.

The former generic endpoint exposed revision rows without applying changes to the
native entity.  It is intentionally closed for every legacy entity type; callers
must address the tenant-scoped AI asset record instead.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services import ai_assets as ai_asset_service

router = APIRouter(prefix="/config-history", tags=["config-history"])


class RollbackRequest(BaseModel):
    target_version: int


def _require_ai_asset(entity_type: str) -> None:
    if entity_type != "ai_asset":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Generic configuration history is retired; use the enterprise AI asset API",
        )


def _tenant_id(user: User) -> uuid.UUID:
    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A selected tenant is required")
    return tenant_id


@router.get("/{entity_type}/{entity_id}")
async def list_revisions(
    entity_type: str,
    entity_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List revision history for a tenant-scoped AI asset (newest first)."""
    _require_ai_asset(entity_type)
    try:
        return await ai_asset_service.revision_history(
            db,
            tenant_id=_tenant_id(current_user),
            asset_id=entity_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{entity_type}/{entity_id}/{version}")
async def get_revision(
    entity_type: str,
    entity_id: uuid.UUID,
    version: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a tenant-scoped AI asset revision."""
    _require_ai_asset(entity_type)
    try:
        rev = await ai_asset_service.revision_detail(
            db,
            tenant_id=_tenant_id(current_user),
            asset_id=entity_id,
            version=version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if not rev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found")
    return rev


@router.post("/{entity_type}/{entity_id}/rollback")
async def rollback_revision(
    entity_type: str,
    entity_id: uuid.UUID,
    body: RollbackRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Apply an AI asset revision to its native entity and create a new revision."""
    _require_ai_asset(entity_type)
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    try:
        _, revision = await ai_asset_service.rollback_asset(
            db,
            tenant_id=_tenant_id(current_user),
            asset_id=entity_id,
            target_version=body.target_version,
            actor_user_id=current_user.id,
        )
    except ValueError as exc:
        await db.rollback()
        await ai_asset_service.record_projection_failure(
            db,
            tenant_id=_tenant_id(current_user),
            asset_id=entity_id,
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
            asset_id=entity_id,
            operation="rollback",
            error=exc,
            actor_user_id=current_user.id,
        )
        await db.commit()
        raise
    return {
        "version": revision.version,
        "message": f"Rolled back to version {body.target_version}",
    }
