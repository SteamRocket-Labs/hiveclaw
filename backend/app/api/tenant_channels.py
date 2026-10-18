"""Tenant-level channel configuration + enterprise webhook (ARCHITECTURE.md Phase 6).

Admin endpoints for managing company-wide bot credentials.
Enterprise webhook endpoint that routes inbound messages by sender → Main Agent.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from pydantic import BaseModel, field_serializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin, get_current_user
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db, pin_rls_tenant_context
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from app.services.channel_secret_storage import redact_channel_extra_config

router = APIRouter(tags=["tenant-channels"])


# ─── Schemas ────────────────────────────────────────────


class TenantChannelConfigOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    channel_type: str
    app_id: str | None = None
    is_active: bool = True
    extra_config: dict = {}

    model_config = {"from_attributes": True}

    @field_serializer("extra_config")
    def serialize_extra_config(self, value: dict) -> dict:
        return redact_channel_extra_config(value)


class TenantChannelConfigUpsert(BaseModel):
    app_id: str
    app_secret: str
    encrypt_key: str | None = None
    verification_token: str | None = None
    extra_config: dict = {}


class TenantChannelReconnect(BaseModel):
    principal_id: uuid.UUID


@router.post("/tenant-channels/feishu/identity/reconnect")
async def reconnect_tenant_feishu_identity(
    body: TenantChannelReconnect, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    from app.services.external_principal_service import ExternalPrincipalAuthorityError, ExternalPrincipalRevokedError
    from app.services.tenant_feishu import reconnect_tenant_feishu_principal

    await resolve_and_pin_tenant_scope(db, current_user)
    try:
        result = await reconnect_tenant_feishu_principal(db, principal_id=body.principal_id, user=current_user)
    except (ExternalPrincipalAuthorityError, ExternalPrincipalRevokedError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    await db.flush()
    return {"principal_id": result.principal.id, "linked_user_id": result.actor.id, "authority_bound": result.actor.authority_bound}


# ─── Admin CRUD ─────────────────────────────────────────


@router.get("/tenant-channels", response_model=list[TenantChannelConfigOut])
async def list_tenant_channels(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all enterprise-level channel configs for the current tenant."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No tenant")
    result = await db.execute(
        select(TenantChannelConfig).where(TenantChannelConfig.tenant_id == current_user.tenant_id)
    )
    return [TenantChannelConfigOut.model_validate(c) for c in result.scalars().all()]


@router.put("/tenant-channels/{channel_type}", response_model=TenantChannelConfigOut)
async def upsert_tenant_channel(
    channel_type: str,
    body: TenantChannelConfigUpsert,
    request: Request,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create or update enterprise-level channel config (admin only).

    Returns the config with a webhook URL for the tenant.
    """
    if not current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No tenant")

    result = await db.execute(
        select(TenantChannelConfig).where(
            TenantChannelConfig.tenant_id == current_user.tenant_id,
            TenantChannelConfig.channel_type == channel_type,
        )
    )
    config = result.scalar_one_or_none()

    if config:
        config.app_id = body.app_id
        config.app_secret = body.app_secret
        config.encrypt_key = body.encrypt_key
        config.verification_token = body.verification_token
        config.extra_config = body.extra_config
    else:
        config = TenantChannelConfig(
            tenant_id=current_user.tenant_id,
            channel_type=channel_type,
            app_id=body.app_id,
            app_secret=body.app_secret,
            encrypt_key=body.encrypt_key,
            verification_token=body.verification_token,
            extra_config=body.extra_config,
            is_active=True,
        )
        db.add(config)

    await db.flush()

    out = TenantChannelConfigOut.model_validate(config)
    return out


@router.delete("/tenant-channels/{channel_type}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tenant_channel(
    channel_type: str,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove enterprise-level channel config (admin only)."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No tenant")
    result = await db.execute(
        select(TenantChannelConfig).where(
            TenantChannelConfig.tenant_id == current_user.tenant_id,
            TenantChannelConfig.channel_type == channel_type,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel config not found")
    await db.delete(config)
    await db.flush()


@router.get("/tenant-channels/{channel_type}/webhook-url")
async def get_tenant_webhook_url(
    channel_type: str,
    request: Request,
    current_user: User = Depends(get_current_admin),
):
    """Return the enterprise webhook URL for this channel type."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No tenant")
    base = str(request.base_url).rstrip("/")
    return {"webhook_url": f"{base}/api/channel/{channel_type}/tenant/{current_user.tenant_id}/webhook"}


# ─── Enterprise Feishu Webhook ──────────────────────────


@router.post("/channel/feishu/tenant/{tenant_id}/webhook")
async def feishu_tenant_webhook(
    tenant_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Enterprise-level Feishu webhook — routes messages by sender to Main Agent.

    This replaces per-agent webhooks for enterprise deployments. One webhook URL
    per company; inbound messages are routed based on the sender's Feishu identity.
    """
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")
    await pin_rls_tenant_context(db, tenant_id)

    # Load tenant channel config
    result = await db.execute(
        select(TenantChannelConfig).where(
            TenantChannelConfig.tenant_id == tenant_id,
            TenantChannelConfig.channel_type == "feishu",
            TenantChannelConfig.is_active.is_(True),
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant Feishu config not found")

    from app.api.feishu import authenticate_feishu_webhook

    body = authenticate_feishu_webhook(config, headers=request.headers, body_str=body_str)

    # Handle challenge AFTER security verification
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    # Extract event
    event = body.get("event", {})
    header = body.get("header", {})
    event_type = header.get("event_type", "")

    if event_type != "im.message.receive_v1":
        return {"ok": True}

    # Extract sender
    sender = event.get("sender", {}).get("sender_id", {})
    sender_user_id = sender.get("user_id", "")
    sender_open_id = sender.get("open_id", "")

    # Route: sender → User → Main Agent
    from app.services.tenant_feishu import tenant_feishu_assistant

    target_agent_id = await tenant_feishu_assistant(db, config=config, sender=sender)
    if not target_agent_id:
        logger.info(f"[tenant-webhook] No Main Agent for sender user_id={sender_user_id} open_id={sender_open_id}")
        # Optionally reply with "not registered" message
        return {"ok": True, "routed": False}

    from app.services.channel_ingress_inbox import accept_authenticated_channel_event, channel_installation_ref

    await accept_authenticated_channel_event(
        db,
        tenant_id=tenant_id,
        agent_id=target_agent_id,
        provider="feishu",
        installation_ref=channel_installation_ref(config, fallback=f"tenant:{tenant_id}:feishu"),
        provider_event_id=str(header.get("event_id") or ""),
        handler_key="feishu.event",
        body=body,
        metadata={
            "transport": "tenant_webhook",
            "tenant_channel_config_id": str(getattr(config, "id", None) or ""),
        },
    )

    return {"ok": True, "routed": True}
