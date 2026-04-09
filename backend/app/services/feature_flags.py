"""Feature flag evaluation service — server-evaluated, Redis-cached."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import get_redis
from app.models.feature_flag import FeatureFlag

logger = logging.getLogger(__name__)

CACHE_TTL = 30  # seconds


async def _get_flag_from_cache(key: str) -> dict | None:
    """Try to get flag from Redis cache."""
    try:
        r = await get_redis()
        cached = await r.get(f"ff:{key}")
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.debug("Feature flag cache miss for %s: %s", key, e)
    return None


async def _set_flag_cache(key: str, data: dict) -> None:
    """Cache flag in Redis."""
    try:
        r = await get_redis()
        await r.set(f"ff:{key}", json.dumps(data, default=str), ex=CACHE_TTL)
    except Exception as e:
        logger.debug("Feature flag cache set failed for %s: %s", key, e)


async def is_enabled(
    db: AsyncSession,
    flag_key: str,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> bool:
    """Evaluate a feature flag for the given context.

    Evaluation order: overrides > allowlist > percentage > boolean.
    """
    # Try cache first
    cached = await _get_flag_from_cache(flag_key)
    if cached is None:
        # Load from DB
        result = await db.execute(select(FeatureFlag).where(FeatureFlag.key == flag_key))
        flag = result.scalar_one_or_none()
        if not flag:
            return False
        cached = {
            "enabled": flag.enabled,
            "flag_type": flag.flag_type,
            "rollout_percentage": flag.rollout_percentage,
            "allowed_tenant_ids": [str(t) for t in flag.allowed_tenant_ids] if flag.allowed_tenant_ids else [],
            "allowed_user_ids": [str(u) for u in flag.allowed_user_ids] if flag.allowed_user_ids else [],
            "overrides": flag.overrides or {},
            "expires_at": flag.expires_at.isoformat() if flag.expires_at else None,
        }
        await _set_flag_cache(flag_key, cached)

    # Check expiry
    expires = cached.get("expires_at")
    if expires:
        try:
            if datetime.fromisoformat(expires) < datetime.now(timezone.utc):
                return False
        except ValueError as e:
            logger.warning("Invalid expires_at format for flag %s: %s", flag_key, e)

    # 1. Check overrides (highest priority)
    overrides = cached.get("overrides", {})
    if tenant_id and f"tenant:{tenant_id}" in overrides:
        return bool(overrides[f"tenant:{tenant_id}"])
    if user_id and f"user:{user_id}" in overrides:
        return bool(overrides[f"user:{user_id}"])

    flag_type = cached.get("flag_type", "boolean")

    # 2. Allowlist
    if flag_type == "allowlist":
        if tenant_id and str(tenant_id) in cached.get("allowed_tenant_ids", []):
            return True
        if user_id and str(user_id) in cached.get("allowed_user_ids", []):
            return True
        return False

    # 3. Tenant gate
    if flag_type == "tenant_gate":
        return tenant_id is not None and str(tenant_id) in cached.get("allowed_tenant_ids", [])

    # 4. Percentage rollout
    if flag_type == "percentage":
        pct = cached.get("rollout_percentage", 0)
        if pct is None or pct <= 0:
            return False
        if pct >= 100:
            return True
        # Deterministic hash: same user always gets same result for same flag
        seed = f"{flag_key}:{user_id or tenant_id or 'global'}"
        hash_val = int(hashlib.md5(seed.encode()).hexdigest(), 16) % 100
        return hash_val < pct

    # 5. Simple boolean
    return cached.get("enabled", False)


async def evaluate_all(
    db: AsyncSession,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> dict[str, bool]:
    """Bulk evaluate all flags for the current user. Used by frontend on login."""
    result = await db.execute(select(FeatureFlag))
    flags = result.scalars().all()
    output = {}
    for flag in flags:
        # Cache each flag individually
        await _set_flag_cache(flag.key, {
            "enabled": flag.enabled,
            "flag_type": flag.flag_type,
            "rollout_percentage": flag.rollout_percentage,
            "allowed_tenant_ids": [str(t) for t in flag.allowed_tenant_ids] if flag.allowed_tenant_ids else [],
            "allowed_user_ids": [str(u) for u in flag.allowed_user_ids] if flag.allowed_user_ids else [],
            "overrides": flag.overrides or {},
            "expires_at": flag.expires_at.isoformat() if flag.expires_at else None,
        })
        output[flag.key] = await is_enabled(db, flag.key, tenant_id=tenant_id, user_id=user_id)
    return output
