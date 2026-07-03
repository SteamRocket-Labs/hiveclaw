"""Desktop Auth Bridge endpoints (ARCHITECTURE.md §7.1).

Handles Feishu OAuth for Desktop clients, JWT refresh via refresh tokens,
and deep-link redirects back to the Desktop app.

Security: OAuth state parameter carries a CSRF nonce (not device_id directly).
The nonce→device_id mapping is stored in Redis (production) or in-process
TTLCache (fallback when Redis is unavailable).
"""

import secrets
from urllib.parse import urlencode

from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    get_current_user,
    revoke_refresh_token,
    verify_refresh_token,
)
from app.database import get_db, pin_rls_tenant_context
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.auth_provider import feishu_auth_provider

settings = get_settings()

router = APIRouter(tags=["desktop-auth"])

FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"

_OAUTH_STATE_TTL = 600  # 10 minutes
_OAUTH_STATE_PREFIX = "oauth_state:"

# In-memory fallback for when Redis is unavailable (dev/single-instance)
_oauth_state_fallback: TTLCache[str, str] = TTLCache(maxsize=10_000, ttl=_OAUTH_STATE_TTL)


async def _store_oauth_state(nonce: str, device_id: str) -> None:
    """Store nonce→device_id in Redis; fall back to in-memory cache."""
    try:
        from app.core.events import get_redis
        r = await get_redis()
        await r.set(f"{_OAUTH_STATE_PREFIX}{nonce}", device_id, ex=_OAUTH_STATE_TTL)
    except Exception:
        logger.debug("[desktop-auth] Redis unavailable for OAuth state, using in-memory fallback")
        _oauth_state_fallback[nonce] = device_id


async def _consume_oauth_state(nonce: str) -> str | None:
    """Atomically get-and-delete nonce from Redis; fall back to in-memory cache.

    Returns the device_id or None if the nonce is invalid/expired.
    """
    try:
        from app.core.events import get_redis
        r = await get_redis()
        # GETDEL is atomic: returns the value and deletes the key in one round-trip
        device_id = await r.getdel(f"{_OAUTH_STATE_PREFIX}{nonce}")
        if device_id is not None:
            return device_id
    except Exception:
        logger.debug("[desktop-auth] Redis unavailable for OAuth state, checking in-memory fallback")

    return _oauth_state_fallback.pop(nonce, None)


# ─── Schemas ────────────────────────────────────────────


class DesktopExchangeRequest(BaseModel):
    refresh_token: str
    device_id: str


class DesktopExchangeResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ─── Endpoints ──────────────────────────────────────────


@router.get("/auth/feishu/authorize")
async def feishu_authorize_for_desktop(
    request: Request,
    device_id: str = Query(..., description="Desktop device identifier"),
):
    """Redirect Desktop user to Feishu OAuth login page.

    Generates a CSRF nonce for the OAuth state parameter and stores the
    nonce→device_id mapping server-side to prevent CSRF attacks.
    """
    if not settings.FEISHU_APP_ID:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Feishu OAuth not configured")

    # Generate CSRF nonce; store mapping server-side via Redis (P0 fix: #2/#3)
    nonce = secrets.token_urlsafe(32)
    await _store_oauth_state(nonce, device_id)

    callback_url = str(request.base_url).rstrip("/") + "/api/auth/feishu/callback-desktop"
    params = {
        "client_id": settings.FEISHU_APP_ID,
        "redirect_uri": callback_url,
        "response_type": "code",
        "state": nonce,
    }
    feishu_url = f"{FEISHU_AUTHORIZE_URL}?{urlencode(params)}"
    return RedirectResponse(url=feishu_url, status_code=status.HTTP_302_FOUND)


@router.get("/auth/feishu/callback-desktop")
async def feishu_callback_desktop(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Handle Feishu OAuth callback for Desktop clients.

    Validates the CSRF nonce, exchanges the code for user info, issues
    JWT + refresh token, and redirects to Desktop via deep link.
    """
    # Validate CSRF nonce via Redis (P0 fix: #2/#3)
    device_id = await _consume_oauth_state(state)
    if device_id is None:
        logger.warning("[desktop-auth] Invalid or expired OAuth state nonce")
        error_url = f"{settings.DESKTOP_DEEP_LINK_SCHEME}://auth/error?reason=invalid_state"
        return RedirectResponse(url=error_url, status_code=status.HTTP_302_FOUND)

    try:
        user, access_token = await feishu_auth_provider.authenticate_with_code(db, code=code, tenant_id=None)
    except Exception as exc:
        # P0 fix #1: never leak internal exception details to client
        logger.error(f"[desktop-auth] Feishu code exchange failed: {exc}")
        error_url = f"{settings.DESKTOP_DEEP_LINK_SCHEME}://auth/error?reason=auth_failed"
        return RedirectResponse(url=error_url, status_code=status.HTTP_302_FOUND)

    await pin_rls_tenant_context(db, user.tenant_id)

    # P1 fix #8: revoke prior refresh tokens for same (user, device) before issuing new one
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.device_id == device_id, RefreshToken.revoked.is_(False))
        .values(revoked=True)
    )
    refresh_token_raw = await create_refresh_token(db, user.id, device_id)

    params = urlencode({
        "token": access_token,
        "refresh_token": refresh_token_raw,
        "user_id": str(user.id),
        "display_name": user.display_name or user.username,
    })
    deep_link = f"{settings.DESKTOP_DEEP_LINK_SCHEME}://auth/callback?{params}"

    logger.info(f"[desktop-auth] User {user.username} authenticated via Feishu, redirecting to Desktop")
    return RedirectResponse(url=deep_link, status_code=status.HTTP_302_FOUND)


@router.post("/auth/desktop/exchange", response_model=DesktopExchangeResponse)
async def exchange_refresh_token(
    body: DesktopExchangeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a valid refresh token for a new access token.

    The old refresh token remains valid until it expires or is explicitly revoked.
    Desktop should call this before the access token expires.
    """
    token_row = await verify_refresh_token(db, body.refresh_token, device_id=body.device_id)

    user = await db.get(User, token_row.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User inactive")

    access_token = create_access_token(
        str(user.id), user.role, tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )
    return DesktopExchangeResponse(access_token=access_token)


@router.post("/auth/desktop/logout", status_code=status.HTTP_204_NO_CONTENT)
async def desktop_logout(
    body: DesktopExchangeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a refresh token on Desktop logout. Requires valid JWT."""
    await revoke_refresh_token(db, body.refresh_token)
