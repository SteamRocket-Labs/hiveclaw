"""Tenant-level channel configuration + enterprise webhook (ARCHITECTURE.md Phase 6).

Admin endpoints for managing company-wide bot credentials.
Enterprise webhook endpoint that routes inbound messages by sender → Main Agent.
"""

from __future__ import annotations

import uuid
import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from loguru import logger
from pydantic import BaseModel, field_serializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin, get_current_user
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db, pin_rls_tenant_context
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from app.services.channel_secret_storage import redact_channel_extra_config
from app.api.channel_secrets import is_masked_secret, resolve_secret_value

router = APIRouter(tags=["tenant-channels"])


# ─── Schemas ────────────────────────────────────────────


class TenantChannelConfigOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    channel_type: str
    app_id: str | None = None
    is_active: bool = True
    extra_config: dict = {}
    revision: str = "unconfigured"
    has_app_secret: bool = False
    has_encrypt_key: bool = False
    has_verification_token: bool = False

    model_config = {"from_attributes": True}

    @field_serializer("extra_config")
    def serialize_extra_config(self, value: dict) -> dict:
        return redact_channel_extra_config({key: item for key, item in value.items() if key != "_settings_revision"})


def _config_revision(config):
    if config is None:
        return "unconfigured"
    nonce = (config.extra_config or {}).get("_settings_revision") or str(getattr(config, "updated_at", "legacy"))
    return hashlib.sha256(json.dumps([str(config.id), config.app_id, config.is_active, nonce]).encode()).hexdigest()


def _config_view(config):
    return TenantChannelConfigOut.model_validate(config).model_copy(update={
        "revision": _config_revision(config), "has_app_secret": bool(config.app_secret),
        "has_encrypt_key": bool(config.encrypt_key), "has_verification_token": bool(config.verification_token),
    })


async def _lock_config(db, tenant_id, channel_type):
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"tenant-channel:{tenant_id}:{channel_type}", 0))))


async def _pin_requested_scope(db, user, expected_tenant_id=None, expected_user_id=None):
    if expected_user_id is not None and expected_user_id != user.id:
        raise HTTPException(409, detail={"code": "tenant_channel_scope_changed"})
    return await resolve_and_pin_tenant_scope(db, user, expected_tenant_id)


class TenantChannelConfigUpsert(BaseModel):
    app_id: str
    app_secret: str | None = None
    encrypt_key: str | None = None
    verification_token: str | None = None
    extra_config: dict | None = None
    expected_revision: str | None = None
    is_active: bool | None = None
    platform_region: Literal["feishu_cn", "lark_global"] | None = None
    expected_tenant_id: uuid.UUID | None = None
    expected_user_id: uuid.UUID | None = None


class TenantChannelReconnect(BaseModel):
    principal_id: uuid.UUID
    expected_tenant_id: uuid.UUID | None = None
    expected_user_id: uuid.UUID | None = None


@router.get("/tenant-channels/feishu/identity")
async def current_tenant_feishu_identity(
    response: Response, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
    expected_tenant_id: uuid.UUID | None = None, expected_user_id: uuid.UUID | None = None,
):
    from app.services.tenant_feishu import current_member_feishu_status

    await _pin_requested_scope(db, current_user, expected_tenant_id, expected_user_id)
    response.headers["Cache-Control"] = "no-store"
    return await current_member_feishu_status(db, user=current_user)


@router.post("/tenant-channels/feishu/identity/disconnect")
async def disconnect_tenant_feishu_identity(
    body: TenantChannelReconnect, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    from app.models.external_principal import ExternalPrincipal, ExternalPrincipalBindingEvent
    from app.services.external_principal_service import unlink_external_principal

    await _pin_requested_scope(db, current_user, body.expected_tenant_id, body.expected_user_id)
    principal = await db.scalar(select(ExternalPrincipal).where(ExternalPrincipal.id == body.principal_id,
        ExternalPrincipal.tenant_id == current_user.tenant_id, ExternalPrincipal.provider == "feishu",
        ExternalPrincipal.tenant_channel_config_id.is_not(None)).with_for_update())
    owned_unlink = principal is not None and principal.linked_user_id is None and await db.scalar(
        select(ExternalPrincipalBindingEvent.id).where(ExternalPrincipalBindingEvent.external_principal_id == principal.id,
            ExternalPrincipalBindingEvent.action == "unlinked", ExternalPrincipalBindingEvent.previous_user_id == current_user.id,
            ExternalPrincipalBindingEvent.actor_user_id == current_user.id).limit(1))
    if principal is None or principal.linked_user_id != current_user.id and not owned_unlink:
        raise HTTPException(403, "Only your own enterprise bot identity can be disconnected")
    await unlink_external_principal(db, tenant_id=current_user.tenant_id, principal_id=principal.id,
        actor_user_id=current_user.id, reason="Authenticated member disconnected their enterprise bot identity")
    await db.flush()
    return {"principal_id": principal.id, "authority_bound": False}


@router.post("/tenant-channels/feishu/identity/reconnect")
async def reconnect_tenant_feishu_identity(
    body: TenantChannelReconnect, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    from app.services.external_principal_service import ExternalPrincipalAuthorityError, ExternalPrincipalRevokedError
    from app.services.tenant_feishu import reconnect_tenant_feishu_principal

    await _pin_requested_scope(db, current_user, body.expected_tenant_id, body.expected_user_id)
    try:
        result = await reconnect_tenant_feishu_principal(db, principal_id=body.principal_id, user=current_user)
    except (ExternalPrincipalAuthorityError, ExternalPrincipalRevokedError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    await db.flush()
    return {"principal_id": result.principal.id, "linked_user_id": result.actor.id, "authority_bound": result.actor.authority_bound}


# ─── Admin CRUD ─────────────────────────────────────────


@router.get("/tenant-channels", response_model=list[TenantChannelConfigOut])
async def list_tenant_channels(
    response: Response,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    expected_tenant_id: uuid.UUID | None = None, expected_user_id: uuid.UUID | None = None,
):
    """List all enterprise-level channel configs for the current tenant."""
    await _pin_requested_scope(db, current_user, expected_tenant_id, expected_user_id)
    response.headers["Cache-Control"] = "no-store"
    result = await db.execute(
        select(TenantChannelConfig).where(TenantChannelConfig.tenant_id == current_user.tenant_id)
    )
    return [_config_view(c) for c in result.scalars().all()]


@router.put("/tenant-channels/{channel_type}", response_model=TenantChannelConfigOut)
async def upsert_tenant_channel(
    channel_type: str,
    body: TenantChannelConfigUpsert,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create or update enterprise-level channel config (admin only).

    Returns the config with a webhook URL for the tenant.
    """
    await _pin_requested_scope(db, current_user, body.expected_tenant_id, body.expected_user_id)
    await _lock_config(db, current_user.tenant_id, channel_type)
    response.headers["Cache-Control"] = "no-store"

    result = await db.execute(
        select(TenantChannelConfig).where(
            TenantChannelConfig.tenant_id == current_user.tenant_id,
            TenantChannelConfig.channel_type == channel_type,
        ).with_for_update()
    )
    config = result.scalar_one_or_none()
    previous_revision = _config_revision(config)
    if body.expected_revision is not None and body.expected_revision != previous_revision:
        raise HTTPException(409, detail={"code": "tenant_channel_revision_conflict"})
    app_id = body.app_id.strip()
    if not app_id or len(app_id) > 255:
        raise HTTPException(422, "A valid app_id is required")
    from app.services.feishu_platform import resolve_feishu_platform

    existing_extra = dict(getattr(config, "extra_config", None) or {})
    extra = dict(body.extra_config if body.extra_config is not None else existing_extra)
    if body.platform_region is not None:
        extra["platform_region"] = body.platform_region
    before_platform, after_platform = resolve_feishu_platform(existing_extra), resolve_feishu_platform(extra)
    replacing_app = config is not None and (config.app_id != app_id or
        (before_platform.region, before_platform.open_api_domain) != (after_platform.region, after_platform.open_api_domain))
    if replacing_app and (not body.app_secret or is_masked_secret(body.app_secret)):
        raise HTTPException(422, "A new application requires its own app_secret")
    secrets = {}
    for name in ("app_secret", "encrypt_key", "verification_token"):
        old = getattr(config, name, None) if not replacing_app else None
        value = getattr(body, name)
        if is_masked_secret(value) and not old:
            raise HTTPException(422, "A saved-secret placeholder cannot configure a new secret")
        secrets[name] = resolve_secret_value(value, old, preserve_missing=True)
    if not secrets["app_secret"]:
        raise HTTPException(422, "app_secret is required")
    active = body.is_active if body.is_active is not None else config.is_active if config else True
    if channel_type == "feishu" and active and not (secrets["encrypt_key"] or secrets["verification_token"]):
        raise HTTPException(422, "Configure callback authentication before enabling the company bot")
    if body.extra_config and any(key in body.extra_config for key in ("_settings_revision", "shared_group_entry")):
        raise HTTPException(422, "Managed channel settings use their dedicated endpoint")
    if config is not None and "shared_group_entry" in (config.extra_config or {}):
        extra["shared_group_entry"] = config.extra_config["shared_group_entry"]
    extra["_settings_revision"] = uuid.uuid4().hex

    if config:
        config.app_id = app_id
        config.is_active = active
        config.extra_config = extra
        for name, value in secrets.items():
            setattr(config, name, value)
    else:
        config = TenantChannelConfig(
            tenant_id=current_user.tenant_id,
            channel_type=channel_type,
            app_id=app_id,
            **secrets,
            extra_config=extra,
            is_active=active,
        )
        db.add(config)

    await db.flush()
    await db.refresh(config)
    from app.models.audit import AuditLog

    db.add(AuditLog(tenant_id=current_user.tenant_id, user_id=current_user.id, action="tenant_channel.settings_updated",
        details={"configuration_id": str(config.id), "channel_type": channel_type, "previous_revision": previous_revision,
            "revision": _config_revision(config), "app_changed": replacing_app, "is_active": config.is_active}))
    await db.flush()
    return _config_view(config)


@router.delete("/tenant-channels/{channel_type}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tenant_channel(
    channel_type: str,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove enterprise-level channel config (admin only)."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No tenant")
    await resolve_and_pin_tenant_scope(db, current_user)
    await _lock_config(db, current_user.tenant_id, channel_type)
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
    response: Response,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    expected_tenant_id: uuid.UUID | None = None, expected_user_id: uuid.UUID | None = None,
):
    """Return the enterprise webhook URL for this channel type."""
    await _pin_requested_scope(db, current_user, expected_tenant_id, expected_user_id)
    response.headers["Cache-Control"] = "no-store"
    base = str(request.base_url).rstrip("/")
    return {"webhook_url": f"{base}/api/channel/{channel_type}/tenant/{current_user.tenant_id}/webhook", "tenant_id": current_user.tenant_id}


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
