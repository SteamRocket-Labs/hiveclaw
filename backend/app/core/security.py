"""Security utilities: JWT, password hashing, and authentication dependencies."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db

if TYPE_CHECKING:
    from app.models.refresh_token import RefreshToken

settings = get_settings()

# Bearer token scheme
security = HTTPBearer()

# Refresh token defaults
REFRESH_TOKEN_EXPIRE_DAYS = 30


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def create_access_token(
    user_id: str, role: str, tenant_id: str | None = None, expires_delta: timedelta | None = None,
) -> str:
    """Create a JWT access token."""
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode = {
        "sub": user_id,
        "role": role,
        "exp": expire,
    }
    if tenant_id:
        to_encode["tid"] = tenant_id
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT access token."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Dependency to get the current authenticated user.

    Supports X-Tenant-Id header for tenant context switching:
    - platform_admin: can operate as any active tenant
    - org_admin / member: can only use their own tenant (header ignored if mismatched)
    """
    from app.models.user import User
    from app.models.tenant import Tenant

    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    # Single query: load user + tenant is_active via LEFT JOIN (no extra round-trip)
    result = await db.execute(
        select(User, Tenant.is_active.label("tenant_is_active"))
        .outerjoin(Tenant, User.tenant_id == Tenant.id)
        .where(User.id == uuid.UUID(user_id))
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    user, tenant_is_active = row[0], row[1]
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    # Block access if the user's company/tenant has been disabled
    if user.tenant_id and tenant_is_active is not None and not tenant_is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Company has been disabled",
        )

    # Tenant context override via X-Tenant-Id header
    requested_tenant = request.headers.get("x-tenant-id")
    if requested_tenant and user.tenant_id and str(user.tenant_id) != requested_tenant:
        if user.role == "platform_admin":
            try:
                target_id = uuid.UUID(requested_tenant)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid X-Tenant-Id")
            target_result = await db.execute(
                select(Tenant.is_active).where(Tenant.id == target_id)
            )
            target_active = target_result.scalar_one_or_none()
            if target_active is None:
                raise HTTPException(status_code=404, detail="Target tenant not found")
            if not target_active:
                raise HTTPException(status_code=403, detail="Target tenant is disabled")
            # Detach user from ORM session and override tenant_id in-memory
            db.expunge(user)
            user.tenant_id = target_id

    return user


async def get_current_admin(current_user=Depends(get_current_user)):
    """Dependency to require admin role (platform_admin or org_admin)."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


# Role hierarchy: higher index = more privileges
ROLE_HIERARCHY = ["member", "org_admin", "platform_admin"]


def require_role(*allowed_roles: str):
    """Factory to create a dependency that checks if the user has one of the allowed roles.

    Usage:
        @router.post("/", dependencies=[Depends(require_role("org_admin", "platform_admin"))])
        async def my_endpoint(...):
    """
    async def _check(current_user=Depends(get_current_user)):
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要以下角色之一: {', '.join(allowed_roles)}",
            )
        return current_user
    return _check


# ---------------------------------------------------------------------------
# Refresh token helpers (Desktop Auth Bridge)
# ---------------------------------------------------------------------------

def _hash_refresh_token(raw_token: str) -> str:
    """SHA-256 hash of the raw refresh token for DB storage."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def create_refresh_token(
    db: AsyncSession,
    user_id: uuid.UUID,
    device_id: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a refresh token, persist its hash, and return the raw token.

    The raw token is returned exactly once; only its SHA-256 hash is stored.
    """
    from app.models.refresh_token import RefreshToken

    raw_token = secrets.token_urlsafe(48)
    token_hash = _hash_refresh_token(raw_token)
    expires_at = datetime.now(timezone.utc) + (expires_delta or timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))

    db.add(RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        device_id=device_id,
        expires_at=expires_at,
    ))
    await db.flush()
    return raw_token


async def verify_refresh_token(db: AsyncSession, raw_token: str, device_id: str | None = None) -> "RefreshToken":
    """Verify a raw refresh token and return the DB row.

    Raises HTTP 401 if the token is invalid, expired, revoked, or
    bound to a different device.
    """
    from app.models.refresh_token import RefreshToken

    token_hash = _hash_refresh_token(raw_token)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked.is_(False),
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    if row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")
    if device_id is not None and row.device_id != device_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device mismatch")
    return row


async def revoke_refresh_token(db: AsyncSession, raw_token: str) -> None:
    """Revoke a refresh token (e.g. on logout)."""
    from app.models.refresh_token import RefreshToken

    token_hash = _hash_refresh_token(raw_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    row = result.scalar_one_or_none()
    if row:
        row.revoked = True
        await db.flush()

