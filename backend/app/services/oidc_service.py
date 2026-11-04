"""OIDC SSO service — discovery, code exchange, and user provisioning.

Supports any standard OIDC provider (Okta, Azure AD, Keycloak, Authing, etc.).
OIDC configuration is stored per-tenant in TenantSetting(key="oidc_config").
"""

import secrets
import time
import uuid

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.database import enter_rls_bypass
from app.models.tenant_setting import TenantSetting
from app.models.user import User

# Cache discovered OIDC metadata per issuer URL with TTL (in-process)
_discovery_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 3600  # 1 hour


class OIDCAccountConflict(ValueError):
    pass


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
        if metadata.get("issuer") != issuer_url:
            raise ValueError("OIDC discovery issuer does not match configured issuer")

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
    nonce: str | None,
) -> dict:
    """Exchange authorization code for tokens and fetch user info.

    Returns dict with: sub, email, name, picture, issuer.
    """
    if not nonce:
        raise ValueError("OIDC nonce is required")
    metadata = await discover_oidc(issuer_url)
    if not metadata.get("jwks_uri") or not metadata.get("issuer"):
        raise ValueError("OIDC provider must publish an issuer and JWKS URI")
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
        if not id_token_raw:
            raise ValueError("OIDC id_token is required")
        from authlib.jose import JsonWebKey, JsonWebToken
        from authlib.oidc.core import CodeIDToken

        jwks_resp = await client.get(metadata["jwks_uri"])
        jwks_resp.raise_for_status()
        keys = JsonWebKey.import_key_set(jwks_resp.json())
        claims = JsonWebToken([
            "RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
            "ES256", "ES384", "ES512", "EdDSA",
        ]).decode(
            id_token_raw, keys, claims_cls=CodeIDToken,
            claims_options={
                "iss": {"essential": True, "value": metadata["issuer"]},
                "aud": {"essential": True, "value": client_id},
                "sub": {"essential": True},
            },
            claims_params={"nonce": nonce, "client_id": client_id},
        )
        claims.validate(leeway=60)

        # Fetch userinfo if endpoint available
        userinfo = {}
        if userinfo_endpoint and access_token:
            info_resp = await client.get(
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if info_resp.status_code == 200:
                userinfo = info_resp.json()
                if userinfo.get("sub") != claims["sub"]:
                    raise ValueError("OIDC userinfo subject does not match id_token")

    # Userinfo may supplement profile fields, but never supplies the identity.
    merged = {**claims, **userinfo}
    return {
        "sub": claims["sub"],
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
    """Find the bound issuer/subject in this tenant or register a new user.

    Returns (user, jwt_token).
    """
    sub = oidc_user["sub"]
    issuer = oidc_user["issuer"]

    result = await db.execute(select(User).where(
        User.oidc_sub == sub, User.oidc_issuer == issuer, User.tenant_id == tenant_id,
    ))
    user = result.scalar_one_or_none()

    if user:
        if not user.is_active:
            raise ValueError("User account is inactive")
        # Update profile info from IdP
        if oidc_user.get("picture"):
            user.avatar_url = oidc_user["picture"]
        await db.flush()
        token = create_access_token(
            str(user.id),
            user.role,
            tenant_id=str(user.tenant_id) if user.tenant_id else None,
        )
        return user, token

    email = oidc_user.get("email", "")
    name = oidc_user.get("name", "")
    username = email.split("@")[0] if email else f"oidc_{sub[:8]}"
    if not email:
        email = f"{username}@oidc.local"

    # Global account uniqueness must also cover rows hidden by the tenant pin.
    async with enter_rls_bypass(db, reason="OIDC account uniqueness check") as bypass_db:
        existing = await bypass_db.execute(select(User.id).where(or_(
            User.oidc_sub == sub, func.lower(User.email) == email.lower(),
        )).limit(1))
        if existing.scalar_one_or_none() is not None:
            raise OIDCAccountConflict("OIDC account conflicts with an existing account")

    if not auto_provision:
        raise ValueError("User not found and auto-provisioning is disabled")

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
