"""Compatibility routes for the governed enterprise AI-asset revision API.

Legacy entity types remain retired. AI-asset requests share source authority,
owner commands and recovery with the enterprise catalog while retaining their
compatibility response format.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import ai_assets as asset_api
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User

router = APIRouter(prefix="/config-history", tags=["config-history"])
RollbackRequest = asset_api.RollbackAssetRequest


def _require_ai_asset(entity_type: str) -> None:
    if entity_type != "ai_asset":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Generic configuration history is retired; use the enterprise AI asset API",
        )


@router.get("/{entity_type}/{entity_id}")
async def list_revisions(
    entity_type: str,
    entity_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List authorized AI-asset revision metadata, newest first."""
    _require_ai_asset(entity_type)
    return await asset_api.list_ai_asset_revisions(
        asset_id=entity_id, limit=50, current_user=current_user, db=db,
    )


@router.get("/{entity_type}/{entity_id}/{version}")
async def get_revision(
    entity_type: str,
    entity_id: uuid.UUID,
    version: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get an AI-asset revision within its current source audience."""
    _require_ai_asset(entity_type)
    return await asset_api.get_ai_asset_revision(
        asset_id=entity_id, version=version, current_user=current_user, db=db,
    )


@router.post("/{entity_type}/{entity_id}/rollback")
async def rollback_revision(
    entity_type: str,
    entity_id: uuid.UUID,
    body: RollbackRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Apply the exact governed owner command and retain the legacy response."""
    _require_ai_asset(entity_type)
    result = await asset_api.rollback_ai_asset(
        asset_id=entity_id, body=body, current_user=current_user, db=db,
    )
    return {
        "version": result["version"],
        "message": f"Rolled back to version {body.target_version}",
    }
