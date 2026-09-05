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
from app.database import enter_rls_bypass, get_db, pin_rls_tenant_context

if TYPE_CHECKING:
    from app.models.refresh_token import RefreshToken
    from app.models.user import User

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
    user_id: str,
    role: str,
    tenant_id: str | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a JWT access token."""
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES))
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


async def _set_session_tenant(db: AsyncSession, tenant_id: uuid.UUID | str | None) -> None:
    """Pin the active DB session and request context to a validated tenant."""
    await pin_rls_tenant_context(db, tenant_id)


async def _load_user_with_tenant_status(db: AsyncSession, user_id: uuid.UUID):
    from app.models.user import User
    from app.models.tenant import Tenant

    result = await db.execute(
        select(User, Tenant.is_active.label("tenant_is_active"))
        .outerjoin(Tenant, User.tenant_id == Tenant.id)
        .where(User.id == user_id)
    )
    return result.first()


def _request_audit_id(request: Request) -> str:
    """Return the request correlation id already established by middleware."""
    request_state = getattr(request, "state", None)
    trace_id = getattr(request_state, "trace_id", None)
    if not trace_id:
        headers = getattr(request, "headers", {})
        trace_id = headers.get("x-trace-id") or headers.get("X-Trace-Id")
    normalized = str(trace_id or uuid.uuid4()).strip()
    return normalized[:128] or str(uuid.uuid4())


async def _audit_platform_admin_tenant_impersonation(
    *,
    request: Request,
    actor_id: uuid.UUID,
    actor_home_tenant_id: uuid.UUID | None,
    target_tenant_id: uuid.UUID,
) -> None:
    """Write the fail-closed operator receipt for a cross-tenant identity frame."""
    request_state = getattr(request, "state", None)
    if request_state is not None and getattr(request_state, "tenant_impersonation_target_id", None) == str(
        target_tenant_id
    ):
        return

    from app.services.audit_logger import write_platform_security_audit_event

    client = getattr(request, "client", None)
    url = getattr(request, "url", None)
    event_id = await write_platform_security_audit_event(
        event_type="tenant_impersonation",
        severity="warn",
        actor_type="user",
        actor_id=actor_id,
        action="tenant_impersonation",
        resource_type="tenant",
        resource_id=target_tenant_id,
        details={
            "actor_role": "platform_admin",
            "actor_home_tenant_id": str(actor_home_tenant_id) if actor_home_tenant_id is not None else None,
            "target_tenant_id": str(target_tenant_id),
            "request_method": str(getattr(request, "method", "")),
            "request_path": str(getattr(url, "path", "")),
        },
        ip_address=str(getattr(client, "host", "")) or None,
        request_id=_request_audit_id(request),
    )
    if request_state is not None:
        request_state.tenant_impersonation_target_id = str(target_tenant_id)
        request_state.tenant_impersonation_audit_event_id = str(event_id)


async def authenticate_request_user(
    db: AsyncSession,
    *,
    jwt_token: str,
    requested_tenant: str | None,
    request: Request,
) -> "User":
    """Canonical live-user authentication with the validated tenant selection.

    This is the single implementation behind ``get_current_user``: decode the
    access token, load the canonical live ``User`` row (bypass lookup only
    for the identity-recovery shapes ``get_current_user`` itself uses), and
    apply the one validated ``X-Tenant-Id`` channel — existence, liveness,
    in-memory selected-tenant override, session repin, and the fail-closed
    impersonation audit for cross-company platform administrators. Browser
    download lanes reuse it so a Bearer/query JWT can never become a second,
    weaker selector policy.
    """

    from app.models.tenant import Tenant

    payload = decode_access_token(jwt_token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user_uuid = uuid.UUID(user_id)
    token_role = payload.get("role", "")
    token_tenant = payload.get("tid")

    # Single query: load user + tenant is_active via LEFT JOIN (no extra round-trip).
    # For platform admins using X-Tenant-Id, TenantMiddleware has already scoped
    # the DB session to the selected target tenant. The admin user's own row can
    # live in a different home tenant, so identity lookup must happen before
    # applying the selected tenant scope to route queries.
    if requested_tenant and token_role == "platform_admin":
        async with enter_rls_bypass(
            db,
            reason="platform-admin identity lookup before selected-tenant override",
            actor_id=user_id,
        ) as bypass_db:
            row = await _load_user_with_tenant_status(bypass_db, user_uuid)
    elif not token_tenant:
        # A newly registered identity has no tenant to pin until it completes
        # self-create/join.  Resolve exactly the signed token subject under an
        # audited scope; downstream route queries remain fail-closed unless
        # this lookup proves that the user now owns a tenant.
        async with enter_rls_bypass(
            db,
            reason="tenantless authenticated identity lookup before company bootstrap",
            actor_id=user_id,
        ) as bypass_db:
            row = await _load_user_with_tenant_status(bypass_db, user_uuid)
    else:
        row = await _load_user_with_tenant_status(db, user_uuid)

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
    if requested_tenant and user.role == "platform_admin":
        try:
            target_id = uuid.UUID(requested_tenant)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid X-Tenant-Id")
        target_result = await db.execute(select(Tenant.is_active).where(Tenant.id == target_id))
        target_active = target_result.scalar_one_or_none()
        if target_active is None:
            raise HTTPException(status_code=404, detail="Target tenant not found")
        if not target_active:
            raise HTTPException(status_code=403, detail="Target tenant is disabled")
        actor_home_tenant_id = user.tenant_id
        is_cross_tenant_impersonation = actor_home_tenant_id is None or actor_home_tenant_id != target_id
        if is_cross_tenant_impersonation:
            # Detach user from ORM session and override tenant_id in-memory.
            # The active DB session is then pinned to the selected tenant for
            # the endpoint's actual tenant-scoped data access.
            db.expunge(user)
            user.tenant_id = target_id
        await _set_session_tenant(db, target_id)
        if is_cross_tenant_impersonation:
            try:
                await _audit_platform_admin_tenant_impersonation(
                    request=request,
                    actor_id=user.id,
                    actor_home_tenant_id=actor_home_tenant_id,
                    target_tenant_id=target_id,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Security audit unavailable; tenant impersonation was not established",
                ) from exc
    elif token_role == "platform_admin" and user.role != "platform_admin":
        # A stale platform token: TenantMiddleware pinned the request tenant
        # from the token's platform_admin claim (the X-Tenant-Id override when
        # present, otherwise the stale tid), but the live canonical user is no
        # longer a platform administrator. That optimistic pin has no
        # authority — restore the live user's actual tenant scope, including
        # None for a tenantless identity, before any route query runs.
        await _set_session_tenant(db, user.tenant_id)
    elif requested_tenant and user.tenant_id and str(user.tenant_id) != requested_tenant:
        # A stale token may still claim platform_admin after the DB role was
        # downgraded. Ignore the selected tenant and restore the user's own
        # tenant scope so downstream route queries do not run under a foreign
        # tenant chosen from an old localStorage value.
        await _set_session_tenant(db, user.tenant_id)
    elif not token_tenant and user.tenant_id:
        # A stale pre-company token can safely recover after join/create once
        # the canonical User row proves the current tenant.
        await _set_session_tenant(db, user.tenant_id)

    return user


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
    return await authenticate_request_user(
        db,
        jwt_token=credentials.credentials,
        requested_tenant=request.headers.get("x-tenant-id"),
        request=request,
    )


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

    db.add(
        RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            device_id=device_id,
            expires_at=expires_at,
        )
    )
    await db.flush()
    return raw_token


async def verify_refresh_token(db: AsyncSession, raw_token: str, device_id: str | None = None) -> "RefreshToken":
    """Verify a raw refresh token and return the DB row.

    Raises HTTP 401 if the token is invalid, expired, revoked, or
    bound to a different device.
    """
    from app.models.refresh_token import RefreshToken
    from app.models.user import User

    token_hash = _hash_refresh_token(raw_token)
    async with enter_rls_bypass(db, reason="refresh token lookup") as bypass_db:
        result = await bypass_db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked.is_(False),
            )
        )
        row = result.scalar_one_or_none()
        tenant_id = None
        if row:
            tenant_result = await bypass_db.execute(select(User.tenant_id).where(User.id == row.user_id))
            tenant_id = tenant_result.scalar_one_or_none()

    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    await pin_rls_tenant_context(db, tenant_id)
    if row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")
    if device_id is not None and row.device_id != device_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device mismatch")
    return row


async def revoke_refresh_token(db: AsyncSession, raw_token: str) -> None:
    """Revoke a refresh token (e.g. on logout)."""
    from app.models.refresh_token import RefreshToken
    from app.models.user import User

    token_hash = _hash_refresh_token(raw_token)
    async with enter_rls_bypass(db, reason="refresh token revoke lookup") as bypass_db:
        result = await bypass_db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        row = result.scalar_one_or_none()
        tenant_id = None
        if row:
            tenant_result = await bypass_db.execute(select(User.tenant_id).where(User.id == row.user_id))
            tenant_id = tenant_result.scalar_one_or_none()

    if row:
        await pin_rls_tenant_context(db, tenant_id)
        row.revoked = True
        await db.flush()
