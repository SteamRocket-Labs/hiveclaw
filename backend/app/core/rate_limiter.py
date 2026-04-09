"""Distributed rate limiting using Redis sorted sets (sliding window).

Replaces in-memory _rate_hits dicts that don't work across multiple workers.
"""

from __future__ import annotations

import logging
import time

from fastapi import HTTPException, Request, status

from app.core.events import get_redis

logger = logging.getLogger(__name__)


async def check_rate_limit(key: str, limit: int, window_seconds: int = 60) -> bool:
    """Check if a request is within rate limits. Returns True if allowed.

    Uses Redis sorted sets for a sliding window counter.
    Key format: ratelimit:{scope}:{identifier}
    """
    r = await get_redis()
    now = time.time()
    window_start = now - window_seconds

    pipe = r.pipeline()
    pipe.zremrangebyscore(key, 0, window_start)  # Remove expired entries
    pipe.zadd(key, {str(now): now})              # Add current request
    pipe.zcard(key)                               # Count requests in window
    pipe.expire(key, window_seconds + 1)          # Auto-cleanup key
    results = await pipe.execute()

    count = results[2]
    if count > limit:
        # Remove the entry we just added (request is rejected)
        await r.zrem(key, str(now))
        return False
    return True


async def rate_limit_or_429(key: str, limit: int, window_seconds: int = 60) -> None:
    """Check rate limit and raise 429 if exceeded."""
    allowed = await check_rate_limit(key, limit, window_seconds)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({limit} requests per {window_seconds}s)",
            headers={"Retry-After": str(window_seconds)},
        )


# ── Pre-built FastAPI dependencies ─────────────────────────

def rate_limit_dependency(scope: str, limit: int, window_seconds: int = 60):
    """Factory for FastAPI route-level rate limiting.

    Usage:
        @router.post("/login", dependencies=[Depends(rate_limit_dependency("auth", 5, 60))])
    """
    async def _check(request: Request):
        # Key by IP for unauthenticated, by user_id for authenticated
        identifier = request.client.host if request.client else "unknown"
        if hasattr(request.state, "tenant_id") and request.state.tenant_id:
            identifier = request.state.tenant_id
        key = f"ratelimit:{scope}:{identifier}"
        await rate_limit_or_429(key, limit, window_seconds)
    return _check
