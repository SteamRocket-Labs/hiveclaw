"""Feature Flags CRUD API — admin-only management of feature flags."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin
from app.database import get_db
from app.models.feature_flag import FeatureFlag
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feature-flags", tags=["feature-flags"])


class FeatureFlagCreate(BaseModel):
    key: str
    description: str = ""
    flag_type: str = "boolean"
    enabled: bool = False
    rollout_percentage: int | None = None
    allowed_tenant_ids: list[uuid.UUID] | None = None
    allowed_user_ids: list[uuid.UUID] | None = None
    overrides: dict | None = None


class FeatureFlagUpdate(BaseModel):
    description: str | None = None
    flag_type: str | None = None
    enabled: bool | None = None
    rollout_percentage: int | None = None
    allowed_tenant_ids: list[uuid.UUID] | None = None
    allowed_user_ids: list[uuid.UUID] | None = None
    overrides: dict | None = None


class FeatureFlagOut(BaseModel):
    id: uuid.UUID
    key: str
    description: str
    flag_type: str
    enabled: bool
    rollout_percentage: int | None = None
    allowed_tenant_ids: list[uuid.UUID] | None = None
    allowed_user_ids: list[uuid.UUID] | None = None
    overrides: dict | None = None
    created_at: str | None = None
    updated_at: str | None = None

    model_config = {"from_attributes": True}


@router.get("/", response_model=list[FeatureFlagOut])
async def list_feature_flags(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all feature flags (admin only)."""
    result = await db.execute(select(FeatureFlag).order_by(FeatureFlag.key))
    flags = result.scalars().all()
    return [_flag_to_out(f) for f in flags]


@router.post("/", response_model=FeatureFlagOut, status_code=status.HTTP_201_CREATED)
async def create_feature_flag(
    data: FeatureFlagCreate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new feature flag (admin only)."""
    existing = await db.execute(select(FeatureFlag).where(FeatureFlag.key == data.key))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Flag with key '{data.key}' already exists")

    flag = FeatureFlag(
        key=data.key,
        description=data.description,
        flag_type=data.flag_type,
        enabled=data.enabled,
        rollout_percentage=data.rollout_percentage,
        allowed_tenant_ids=data.allowed_tenant_ids,
        allowed_user_ids=data.allowed_user_ids,
        overrides=data.overrides,
        created_by=current_user.id,
    )
    db.add(flag)
    await db.commit()
    await db.refresh(flag)

    # Invalidate cache
    await _invalidate_flag_cache(flag.key)

    return _flag_to_out(flag)


@router.patch("/{flag_id}", response_model=FeatureFlagOut)
async def update_feature_flag(
    flag_id: uuid.UUID,
    data: FeatureFlagUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update an existing feature flag (admin only)."""
    result = await db.execute(select(FeatureFlag).where(FeatureFlag.id == flag_id))
    flag = result.scalar_one_or_none()
    if not flag:
        raise HTTPException(status_code=404, detail="Feature flag not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(flag, field, value)

    await db.commit()
    await db.refresh(flag)

    await _invalidate_flag_cache(flag.key)

    return _flag_to_out(flag)


@router.delete("/{flag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feature_flag(
    flag_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a feature flag (admin only)."""
    result = await db.execute(select(FeatureFlag).where(FeatureFlag.id == flag_id))
    flag = result.scalar_one_or_none()
    if not flag:
        raise HTTPException(status_code=404, detail="Feature flag not found")

    key = flag.key
    await db.delete(flag)
    await db.commit()

    await _invalidate_flag_cache(key)


def _flag_to_out(flag: FeatureFlag) -> FeatureFlagOut:
    return FeatureFlagOut(
        id=flag.id,
        key=flag.key,
        description=flag.description,
        flag_type=flag.flag_type,
        enabled=flag.enabled,
        rollout_percentage=flag.rollout_percentage,
        allowed_tenant_ids=flag.allowed_tenant_ids,
        allowed_user_ids=flag.allowed_user_ids,
        overrides=flag.overrides,
        created_at=flag.created_at.isoformat() if flag.created_at else None,
        updated_at=flag.updated_at.isoformat() if flag.updated_at else None,
    )


async def _invalidate_flag_cache(key: str) -> None:
    """Remove a flag from Redis cache so the next evaluation reads from DB."""
    try:
        from app.core.events import get_redis
        r = await get_redis()
        await r.delete(f"ff:{key}")
    except Exception as e:
        logger.debug("Failed to invalidate flag cache for %s: %s", key, e)
