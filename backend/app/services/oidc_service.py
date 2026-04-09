"""OIDC SSO service — discovery, code exchange, and user provisioning.

Supports any standard OIDC provider (Okta, Azure AD, Keycloak, Authing, etc.).
OIDC configuration is stored per-tenant in TenantSetting(key="oidc_config").
"""

import base64
import json
import logging
import secrets
import time
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.tenant_setting import TenantSetting
from app.models.user import User

logger = logging.getLogger(__name__)

# Cache discovered OIDC metadata per issuer URL with TTL (in-process)
_discovery_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 3600  # 1 hour


async def discover_oidc(issuer_url: str) -> dict:
    """Fetch OIDC discovery document from issuer's well-known endpoint."""
    cached = _discovery_cache.get(issuer_url)
    if cached and (time.time() - cached[1]) < _CACHE_TTL:
        return cached[0]

    well_known = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(well_known)
        resp.raise_for_status()
        metadata = resp.json()

    _discovery_cache[issuer_url] = (metadata, time.time())
    return metadata


async def get_tenant_oidc_config(db: AsyncSession, tenant_id: uuid.UUID) -> dict | None:
    """Load OIDC config from TenantSetting for the given tenant."""
    result = await db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == tenant_id,
            TenantSetting.key == "oidc_config",
        )
    )
    setting = result.scalar_one_or_none()
    if not setting or not setting.value:
        return None

    cfg = setting.value
    if not cfg.get("issuer_url") or not cfg.get("client_id"):
        return None
    return cfg


async def exchange_code(
    issuer_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict:
    """Exchange authorization code for tokens and fetch user info.

    Returns dict with: sub, email, name, picture, issuer.
    """
    metadata = await discover_oidc(issuer_url)
    token_endpoint = metadata["token_endpoint"]
    userinfo_endpoint = metadata.get("userinfo_endpoint")

    async with httpx.AsyncClient(timeout=15) as client:
        # Token exchange
        token_resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()

        access_token = token_data.get("access_token", "")
        id_token_raw = token_data.get("id_token")

        # Verify and decode id_token using JWKS from the IdP
        claims = {}
        if id_token_raw:
            from authlib.jose import JsonWebKey, jwt as authlib_jwt

            jwks_uri = metadata.get("jwks_uri")
            if jwks_uri:
                jwks_resp = await client.get(jwks_uri)
                jwks_resp.raise_for_status()
                keys = JsonWebKey.import_key_set(jwks_resp.json())
                claims = authlib_jwt.decode(id_token_raw, keys)
                claims.validate()
            else:
                # Fallback: decode without verification (not recommended)
                parts = id_token_raw.split(".")
                if len(parts) >= 2:
                    payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
                    claims = json.loads(base64.urlsafe_b64decode(payload))
                logger.warning("OIDC issuer has no jwks_uri — id_token not cryptographically verified")

        # Fetch userinfo if endpoint available
        userinfo = {}
        if userinfo_endpoint and access_token:
            info_resp = await client.get(
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if info_resp.status_code == 200:
                userinfo = info_resp.json()

    # Merge id_token claims and userinfo (userinfo takes precedence)
    merged = {**claims, **userinfo}
    return {
        "sub": merged.get("sub", ""),
        "email": merged.get("email", ""),
        "name": merged.get("name", merged.get("preferred_username", "")),
        "picture": merged.get("picture", ""),
        "issuer": issuer_url,
    }


async def login_or_register(
    db: AsyncSession,
    oidc_user: dict,
    tenant_id: uuid.UUID,
    auto_provision: bool = True,
) -> tuple[User, str]:
    """Find existing user by oidc_sub or register a new one.

    Returns (user, jwt_token).
    """
    sub = oidc_user["sub"]
    issuer = oidc_user["issuer"]

    # Look up by oidc_sub (tenant-scoped to prevent cross-tenant identity confusion)
    result = await db.execute(select(User).where(User.oidc_sub == sub, User.tenant_id == tenant_id))
    user = result.scalar_one_or_none()

    if user:
        # Update profile info from IdP
        if oidc_user.get("picture"):
            user.avatar_url = oidc_user["picture"]
        user.oidc_issuer = issuer
        await db.flush()
        token = create_access_token(
            str(user.id),
            user.role,
            tenant_id=str(user.tenant_id) if user.tenant_id else None,
        )
        return user, token

    # Try to match by email (tenant-scoped)
    email = oidc_user.get("email", "")
    if email:
        result = await db.execute(select(User).where(User.email == email, User.tenant_id == tenant_id))
        user = result.scalar_one_or_none()
        if user:
            # Bind OIDC identity to existing user
            user.oidc_sub = sub
            user.oidc_issuer = issuer
            if oidc_user.get("picture") and not user.avatar_url:
                user.avatar_url = oidc_user["picture"]
            await db.flush()
            token = create_access_token(
                str(user.id),
                user.role,
                tenant_id=str(user.tenant_id) if user.tenant_id else None,
            )
            return user, token

    if not auto_provision:
        raise ValueError("User not found and auto-provisioning is disabled")

    # Create new user
    name = oidc_user.get("name", "")
    username = email.split("@")[0] if email else f"oidc_{sub[:8]}"
    if not email:
        email = f"{username}@oidc.local"

    # Ensure unique username
    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none():
        username = f"{username}_{sub[:6]}"

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(secrets.token_hex(32)),  # random unusable password for OIDC-only users
        display_name=name or username,
        avatar_url=oidc_user.get("picture"),
        oidc_sub=sub,
        oidc_issuer=issuer,
        tenant_id=tenant_id,
        role="member",
    )
    db.add(user)
    await db.flush()

    # Auto-create Participant identity
    from app.models.participant import Participant

    db.add(
        Participant(
            type="user",
            ref_id=user.id,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
        )
    )
    await db.flush()

    token = create_access_token(
        str(user.id),
        user.role,
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )
    return user, token
