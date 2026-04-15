"""Tenant-level channel configuration + enterprise webhook (ARCHITECTURE.md Phase 6).

Admin endpoints for managing company-wide bot credentials.
Enterprise webhook endpoint that routes inbound messages by sender → Main Agent.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin
from app.database import get_db
from app.models.agent import Agent
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from app.services.channel_user_service import channel_user_service

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


class TenantChannelConfigUpsert(BaseModel):
    app_id: str
    app_secret: str
    encrypt_key: str | None = None
    verification_token: str | None = None
    extra_config: dict = {}


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
    import json as _json_tw
    body = _json_tw.loads(body_str)

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
        if "challenge" in body:
            # Allow the initial Feishu URL verification handshake before the
            # tenant config has been persisted.
            return {"challenge": body["challenge"]}
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant Feishu config not found")

    # Security: verify signature → decrypt → then handle challenge/events
    if config.encrypt_key:
        from app.api.feishu import _verify_feishu_signature, _decrypt_feishu_payload

        sig = request.headers.get("X-Lark-Signature", "")
        ts = request.headers.get("X-Lark-Request-Timestamp", "")
        nonce = request.headers.get("X-Lark-Request-Nonce", "")
        if not sig or not _verify_feishu_signature(config.encrypt_key, ts, nonce, body_str, sig):
            logger.warning(f"[tenant-webhook] Feishu signature mismatch for tenant {tenant_id}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

        if "encrypt" in body:
            try:
                body = _decrypt_feishu_payload(config.encrypt_key, body["encrypt"])
            except Exception as exc:
                logger.warning(f"[tenant-webhook] Failed to decrypt for tenant {tenant_id}: {exc}")
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid encrypted payload") from exc
    elif config.verification_token:
        if body.get("token") != config.verification_token:
            logger.warning(f"[tenant-webhook] Verification token mismatch for tenant {tenant_id}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verification token")

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
    target_agent_id = await _resolve_sender_agent(db, tenant_id, sender_user_id, sender_open_id)
    if not target_agent_id:
        logger.info(f"[tenant-webhook] No Main Agent for sender user_id={sender_user_id} open_id={sender_open_id}")
        # Optionally reply with "not registered" message
        return {"ok": True, "routed": False}

    # Delegate to existing per-agent event processing
    from app.api.feishu import process_feishu_event
    await process_feishu_event(target_agent_id, body, db, tenant_channel_config=config)

    return {"ok": True, "routed": True}



async def _resolve_sender_agent(
    db: AsyncSession, tenant_id: uuid.UUID, feishu_user_id: str, feishu_open_id: str,
) -> uuid.UUID | None:
    """Resolve a Feishu sender to their agent within the tenant.

    Lookup chain: feishu_user_id → User → owned Agent
    Fallback: feishu_open_id → User → owned Agent
    """
    user = await channel_user_service.resolve_feishu_user(
        db,
        tenant_id=tenant_id,
        provider_user_id=feishu_user_id or None,
        provider_open_id=feishu_open_id or None,
    )

    if not user:
        return None

    # Find user's owned agent
    result = await db.execute(
        select(Agent.id).where(
            Agent.owner_user_id == user.id,
            Agent.tenant_id == tenant_id,
        ).limit(1)
    )
    return result.scalar_one_or_none()
