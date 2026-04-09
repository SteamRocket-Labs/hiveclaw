"""Tenant-level channel configuration + enterprise webhook (ARCHITECTURE.md Phase 6).

Admin endpoints for managing company-wide bot credentials.
Enterprise webhook endpoint that routes inbound messages by sender → Main Agent.
"""

from __future__ import annotations

import hashlib
import hmac
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
    body = await request.json()

    # Handle Feishu URL verification challenge
    if "challenge" in body:
        return {"challenge": body["challenge"]}

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

    # Verify signature if encrypt_key is set
    if config.encrypt_key:
        timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
        nonce = request.headers.get("X-Lark-Request-Nonce", "")
        signature = request.headers.get("X-Lark-Signature", "")
        raw_body = (await request.body()).decode("utf-8")

        expected = _compute_feishu_signature(timestamp, nonce, config.encrypt_key, raw_body)
        if not hmac.compare_digest(signature, expected):
            logger.warning(f"[tenant-webhook] Feishu signature mismatch for tenant {tenant_id}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

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


# ─── Helpers ────────────────────────────────────────────


def _compute_feishu_signature(timestamp: str, nonce: str, encrypt_key: str, body: str) -> str:
    """Compute Feishu webhook signature (HMAC-SHA256)."""
    content = timestamp + nonce + encrypt_key + body
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def _resolve_sender_agent(
    db: AsyncSession, tenant_id: uuid.UUID, feishu_user_id: str, feishu_open_id: str,
) -> uuid.UUID | None:
    """Resolve a Feishu sender to their agent within the tenant.

    Lookup chain: feishu_user_id → User → owned Agent
    Fallback: feishu_open_id → User → owned Agent
    """
    user = None

    if feishu_user_id:
        result = await db.execute(
            select(User).where(User.feishu_user_id == feishu_user_id, User.tenant_id == tenant_id)
        )
        user = result.scalar_one_or_none()

    if not user and feishu_open_id:
        result = await db.execute(
            select(User).where(User.feishu_open_id == feishu_open_id, User.tenant_id == tenant_id)
        )
        user = result.scalar_one_or_none()

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
