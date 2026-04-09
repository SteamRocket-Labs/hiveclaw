"""Configuration versioning API — history, diff, rollback for agents, skills, prompts."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services import config_versioning

router = APIRouter(prefix="/config-history", tags=["config-history"])


class RollbackRequest(BaseModel):
    target_version: int


@router.get("/{entity_type}/{entity_id}")
async def list_revisions(
    entity_type: str,
    entity_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List revision history for an entity (newest first)."""
    return await config_versioning.get_history(db, entity_type, entity_id)


@router.get("/{entity_type}/{entity_id}/{version}")
async def get_revision(
    entity_type: str,
    entity_id: uuid.UUID,
    version: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get full content of a specific revision."""
    rev = await config_versioning.get_revision(db, entity_type, entity_id, version)
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
    """Rollback to a previous version (creates a new revision with old content)."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    rev = await config_versioning.rollback(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        tenant_id=current_user.tenant_id,
        target_version=body.target_version,
        rolled_back_by_user_id=current_user.id,
    )
    if not rev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target version not found")
    await db.commit()
    return {"version": rev.version, "message": f"Rolled back to version {body.target_version}"}
