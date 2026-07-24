"""Platform-only management API for the global feature rollout catalog."""

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_role
from app.database import get_db, schedule_after_commit
from app.models.feature_flag import FeatureFlag
from app.models.user import User
from app.services.audit_logger import write_platform_security_audit_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feature-flags", tags=["feature-flags"])

FeatureFlagType = Literal["boolean", "percentage", "allowlist", "tenant_gate"]
_FLAG_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,99}$")
_OVERRIDE_KEY_PATTERN = re.compile(r"^(tenant|user):(.+)$")


def _validate_override_map(value: dict[str, bool] | None) -> dict[str, bool] | None:
    if value is None:
        return None
    for key, enabled in value.items():
        match = _OVERRIDE_KEY_PATTERN.fullmatch(key)
        if match is None:
            raise ValueError("Override keys must use tenant:<uuid> or user:<uuid>")
        try:
            uuid.UUID(match.group(2))
        except ValueError as exc:
            raise ValueError(f"Override key has an invalid UUID: {key}") from exc
        if not isinstance(enabled, bool):
            raise ValueError(f"Override value must be boolean: {key}")
    return value


class FeatureFlagCreate(BaseModel):
    key: str = Field(min_length=3, max_length=100)
    description: str = Field(default="", max_length=2000)
    flag_type: FeatureFlagType = "boolean"
    enabled: bool = False
    rollout_percentage: int | None = Field(default=None, ge=0, le=100)
    allowed_tenant_ids: list[uuid.UUID] | None = None
    allowed_user_ids: list[uuid.UUID] | None = None
    overrides: dict[str, bool] | None = None
    expires_at: datetime | None = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if _FLAG_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError("Feature flag key must use lowercase letters, digits, and underscores")
        return value

    @field_validator("overrides")
    @classmethod
    def validate_overrides(cls, value: dict[str, bool] | None) -> dict[str, bool] | None:
        return _validate_override_map(value)

    @model_validator(mode="after")
    def validate_targeting(self):
        if self.flag_type == "percentage" and self.rollout_percentage is None:
            raise ValueError("Percentage rollout requires rollout_percentage")
        return self


class FeatureFlagUpdate(BaseModel):
    expected_updated_at: datetime
    description: str | None = Field(default=None, max_length=2000)
    flag_type: FeatureFlagType | None = None
    enabled: bool | None = None
    rollout_percentage: int | None = Field(default=None, ge=0, le=100)
    allowed_tenant_ids: list[uuid.UUID] | None = None
    allowed_user_ids: list[uuid.UUID] | None = None
    overrides: dict[str, bool] | None = None
    expires_at: datetime | None = None

    @field_validator("overrides")
    @classmethod
    def validate_overrides(cls, value: dict[str, bool] | None) -> dict[str, bool] | None:
        return _validate_override_map(value)

    @model_validator(mode="after")
    def validate_non_empty_mutation(self):
        if not (self.model_fields_set - {"expected_updated_at"}):
            raise ValueError("Feature flag update requires at least one changed field")
        return self


class FeatureFlagOut(BaseModel):
    id: uuid.UUID
    key: str
    description: str
    flag_type: str
    enabled: bool
    rollout_percentage: int | None = None
    allowed_tenant_ids: list[uuid.UUID] | None = None
    allowed_user_ids: list[uuid.UUID] | None = None
    overrides: dict[str, bool] | None = None
    expires_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    model_config = {"from_attributes": True}


@router.get("/", response_model=list[FeatureFlagOut])
async def list_feature_flags(
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """List the global rollout catalog (platform operators only)."""
    result = await db.execute(select(FeatureFlag).order_by(FeatureFlag.key))
    flags = result.scalars().all()
    return [_flag_to_out(f) for f in flags]


@router.post("/", response_model=FeatureFlagOut, status_code=status.HTTP_201_CREATED)
async def create_feature_flag(
    data: FeatureFlagCreate,
    request: Request,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create a global feature flag after durable operator authorization evidence."""
    existing = await db.execute(select(FeatureFlag).where(FeatureFlag.key == data.key))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Flag with key '{data.key}' already exists")

    flag_id = uuid.uuid4()
    after = _state_from_payload(data.model_dump())
    await _write_mutation_audit(
        request=request,
        current_user=current_user,
        action="feature_flag.create",
        flag_id=flag_id,
        key=data.key,
        before=None,
        after=after,
    )
    flag = FeatureFlag(
        id=flag_id,
        key=data.key,
        description=data.description,
        flag_type=data.flag_type,
        enabled=data.enabled,
        rollout_percentage=data.rollout_percentage,
        allowed_tenant_ids=data.allowed_tenant_ids,
        allowed_user_ids=data.allowed_user_ids,
        overrides=data.overrides,
        expires_at=data.expires_at,
        created_by=current_user.id,
    )
    db.add(flag)
    await db.flush()
    _schedule_cache_invalidation(db, flag.key)

    return _flag_to_out(flag)


@router.patch("/{flag_id}", response_model=FeatureFlagOut)
async def update_feature_flag(
    flag_id: uuid.UUID,
    data: FeatureFlagUpdate,
    request: Request,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update a global feature flag after validating the complete targeting state."""
    result = await db.execute(select(FeatureFlag).where(FeatureFlag.id == flag_id).with_for_update())
    flag = result.scalar_one_or_none()
    if not flag:
        raise HTTPException(status_code=404, detail="Feature flag not found")

    update_data = data.model_dump(exclude_unset=True)
    expected_updated_at = update_data.pop("expected_updated_at")
    if not _timestamps_equal(flag.updated_at, expected_updated_at):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "feature_flag_version_conflict",
                "expected_updated_at": expected_updated_at.isoformat(),
                "current_updated_at": flag.updated_at.isoformat(),
            },
        )
    before = _state_from_flag(flag)
    after = dict(before)
    after.update(update_data)
    _validate_complete_state(after)
    await _write_mutation_audit(
        request=request,
        current_user=current_user,
        action="feature_flag.update",
        flag_id=flag.id,
        key=flag.key,
        before=before,
        after=_state_from_payload(after),
    )
    for field, value in update_data.items():
        setattr(flag, field, value)

    await db.flush()
    await db.refresh(flag)
    _schedule_cache_invalidation(db, flag.key)

    return _flag_to_out(flag)


@router.delete("/{flag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feature_flag(
    flag_id: uuid.UUID,
    request: Request,
    expected_updated_at: datetime = Query(),
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a global feature flag after recording the exact prior state."""
    result = await db.execute(select(FeatureFlag).where(FeatureFlag.id == flag_id).with_for_update())
    flag = result.scalar_one_or_none()
    if not flag:
        raise HTTPException(status_code=404, detail="Feature flag not found")
    if not _timestamps_equal(flag.updated_at, expected_updated_at):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "feature_flag_version_conflict",
                "expected_updated_at": expected_updated_at.isoformat(),
                "current_updated_at": flag.updated_at.isoformat(),
            },
        )

    key = flag.key
    await _write_mutation_audit(
        request=request,
        current_user=current_user,
        action="feature_flag.delete",
        flag_id=flag.id,
        key=flag.key,
        before=_state_from_flag(flag),
        after=None,
    )
    await db.delete(flag)
    await db.flush()
    _schedule_cache_invalidation(db, key)


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
        expires_at=flag.expires_at.isoformat() if flag.expires_at else None,
        created_at=flag.created_at.isoformat() if flag.created_at else None,
        updated_at=flag.updated_at.isoformat() if flag.updated_at else None,
    )


def _state_from_payload(payload: dict) -> dict:
    state = dict(payload)
    for field in ("allowed_tenant_ids", "allowed_user_ids"):
        values = state.get(field)
        state[field] = [str(value) for value in values] if values else None
    expires_at = state.get("expires_at")
    if isinstance(expires_at, datetime):
        state["expires_at"] = expires_at.isoformat()
    return state


def _state_from_flag(flag: FeatureFlag) -> dict:
    return _state_from_payload(
        {
            "description": flag.description,
            "flag_type": flag.flag_type,
            "enabled": flag.enabled,
            "rollout_percentage": flag.rollout_percentage,
            "allowed_tenant_ids": flag.allowed_tenant_ids,
            "allowed_user_ids": flag.allowed_user_ids,
            "overrides": flag.overrides,
            "expires_at": flag.expires_at,
        }
    )


def _validate_complete_state(state: dict) -> None:
    flag_type = state.get("flag_type")
    if flag_type not in {"boolean", "percentage", "allowlist", "tenant_gate"}:
        raise HTTPException(status_code=422, detail="Unsupported feature flag type")
    rollout_percentage = state.get("rollout_percentage")
    if flag_type == "percentage" and rollout_percentage is None:
        raise HTTPException(status_code=422, detail="Percentage rollout requires rollout_percentage")
    _validate_override_map(state.get("overrides"))


def _timestamps_equal(left: datetime, right: datetime) -> bool:
    def normalized(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    return normalized(left) == normalized(right)


async def _write_mutation_audit(
    *,
    request: Request,
    current_user: User,
    action: str,
    flag_id: uuid.UUID,
    key: str,
    before: dict | None,
    after: dict | None,
) -> uuid.UUID:
    try:
        return await write_platform_security_audit_event(
            event_type="feature_flag_mutation",
            severity="warning",
            actor_type="user",
            actor_id=current_user.id,
            action=action,
            resource_type="feature_flag",
            resource_id=flag_id,
            details={
                "key": key,
                "before": before,
                "after": after,
            },
            ip_address=request.client.host if request.client else None,
            request_id=getattr(request.state, "request_id", None),
        )
    except Exception as exc:
        logger.exception("Platform audit unavailable for %s on feature flag %s", action, key)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Platform audit unavailable",
        ) from exc


def _schedule_cache_invalidation(db: AsyncSession, key: str) -> None:
    schedule_after_commit(
        db,
        lambda: _invalidate_flag_cache(key),
        description=f"invalidate feature flag {key}",
    )


async def _invalidate_flag_cache(key: str) -> None:
    """Remove a flag from Redis cache so the next evaluation reads from DB."""
    try:
        from app.core.events import get_redis

        r = await get_redis()
        await r.delete(f"ff:{key}")
    except Exception as e:
        logger.debug("Failed to invalidate flag cache for %s: %s", key, e)
