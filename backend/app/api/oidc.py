"""OIDC SSO API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.schemas import TokenResponse, UserOut

router = APIRouter(tags=["oidc"])


class OIDCCallbackRequest(BaseModel):
    code: str
    redirect_uri: str
    tenant_id: str | None = None


class OIDCBindRequest(BaseModel):
    code: str
    redirect_uri: str


# ─── Public: get OIDC config for login page ────────────────────


@router.get("/auth/oidc/config")
async def get_oidc_config(
    tenant_slug: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Public endpoint — returns OIDC config for the login page SSO button.

    Only exposes non-secret fields: issuer_url, client_id, scopes, display_name.
    """
    from app.models.tenant import Tenant
    from app.models.tenant_setting import TenantSetting
    from sqlalchemy import select

    # Find tenant by slug (default if not specified)
    slug = tenant_slug or "default"
    result = await db.execute(select(Tenant).where(Tenant.slug == slug))
    tenant = result.scalar_one_or_none()
    if not tenant:
        return {"configured": False}

    result = await db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == tenant.id,
            TenantSetting.key == "oidc_config",
        )
    )
    setting = result.scalar_one_or_none()
    if not setting or not setting.value:
        return {"configured": False}

    cfg = setting.value
    if not cfg.get("issuer_url") or not cfg.get("client_id"):
        return {"configured": False}

    # Discover authorization endpoint
    from app.services.oidc_service import discover_oidc

    try:
        metadata = await discover_oidc(cfg["issuer_url"])
        auth_endpoint = metadata.get("authorization_endpoint", "")
    except Exception as e:
        logger.warning(f"OIDC discovery failed for {cfg['issuer_url']}: {e}")
        return {"configured": False, "error": "Discovery failed"}

    return {
        "configured": True,
        "issuer_url": cfg["issuer_url"],
        "client_id": cfg["client_id"],
        "scopes": cfg.get("scopes", "openid profile email"),
        "authorization_endpoint": auth_endpoint,
        "display_name": cfg.get("display_name", "SSO"),
        "tenant_id": str(tenant.id),
    }


# ─── OIDC Callback: code exchange + login/register ─────────────


@router.post("/auth/oidc/callback", response_model=TokenResponse)
async def oidc_callback(
    data: OIDCCallbackRequest,
    db: AsyncSession = Depends(get_db),
):
    """Handle OIDC callback — exchange code for tokens, login or register user."""
    from app.models.tenant import Tenant
    from app.services.oidc_service import exchange_code, get_tenant_oidc_config, login_or_register
    from sqlalchemy import select

    # Resolve tenant
    if data.tenant_id:
        tenant_uuid = uuid.UUID(data.tenant_id)
    else:
        # Default tenant
        result = await db.execute(select(Tenant).where(Tenant.slug == "default"))
        tenant = result.scalar_one_or_none()
        if not tenant:
            raise HTTPException(status_code=400, detail="No default tenant found")
        tenant_uuid = tenant.id

    # Load OIDC config
    cfg = await get_tenant_oidc_config(db, tenant_uuid)
    if not cfg:
        raise HTTPException(status_code=400, detail="OIDC not configured for this tenant")

    # Exchange code
    try:
        oidc_user = await exchange_code(
            issuer_url=cfg["issuer_url"],
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            code=data.code,
            redirect_uri=data.redirect_uri,
        )
    except Exception as e:
        logger.error(f"OIDC code exchange failed: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail="OIDC authentication failed")

    if not oidc_user.get("sub"):
        raise HTTPException(status_code=400, detail="OIDC provider did not return a subject identifier")

    # Login or register
    auto_provision = cfg.get("auto_provision", True)
    try:
        user, token = await login_or_register(db, oidc_user, tenant_uuid, auto_provision)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    # Audit event
    try:
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="auth.oidc_login",
            severity="info",
            actor_type="user",
            actor_id=user.id,
            tenant_id=user.tenant_id or tenant_uuid,
            action="oidc_login",
            details={"issuer": cfg["issuer_url"], "sub": oidc_user["sub"]},
        )
    except Exception:
        logger.warning("Audit write failed for auth.oidc_login", exc_info=True)

    await db.commit()

    needs_setup = user.tenant_id is None
    return TokenResponse(
        access_token=token,
        user=UserOut.model_validate(user),
        needs_company_setup=needs_setup,
    )


# ─── Bind OIDC to existing account ─────────────────────────────


@router.post("/auth/oidc/bind")
async def bind_oidc_account(
    data: OIDCBindRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bind OIDC identity to an existing authenticated user."""
    from app.services.oidc_service import exchange_code, get_tenant_oidc_config

    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="User has no tenant")

    cfg = await get_tenant_oidc_config(db, current_user.tenant_id)
    if not cfg:
        raise HTTPException(status_code=400, detail="OIDC not configured for this tenant")

    try:
        oidc_user = await exchange_code(
            issuer_url=cfg["issuer_url"],
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            code=data.code,
            redirect_uri=data.redirect_uri,
        )
    except Exception as e:
        logger.error(f"OIDC bind code exchange failed: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail="OIDC authentication failed")

    current_user.oidc_sub = oidc_user["sub"]
    current_user.oidc_issuer = oidc_user["issuer"]
    await db.flush()

    return UserOut.model_validate(current_user)
