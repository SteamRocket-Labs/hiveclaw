"""Nonsecret channel configuration readback for an original-work question."""

import uuid
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import select

from app.models.channel_config import ChannelConfig
from app.models.tenant_channel_config import TenantChannelConfig


async def channel_configuration_snapshot(db, *, agent_id, channel_type):
    if channel_type == "feishu":
        from app.services.agent_tool_domains.feishu_helpers import load_feishu_channel_configuration

        config = await load_feishu_channel_configuration(db, agent_id=agent_id)
    else:
        config = await db.scalar(select(ChannelConfig).where(ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == channel_type, ChannelConfig.is_configured.is_(True)))
    if config is None:
        return None
    if channel_type == "wechat_personal":
        from app.services.wechat_personal_service import get_channel_identity_status

        identity = await get_channel_identity_status(db, config)
        if identity["identity_status"] != "verified" or identity["requires_access_recovery"]:
            return None
    return {"id": str(config.id), "updated_at": config.updated_at.astimezone(timezone.utc).isoformat(),
        "channel_type": config.channel_type, "scope": "company" if isinstance(config, TenantChannelConfig) else "employee"}


async def validate_channel_configuration_answer(db, *, payload):
    descriptor = payload["configuration_request"]
    ready = payload.get("configuration_ready")
    if not ready:
        raise HTTPException(status_code=409, detail={"code": "channel_configuration_not_confirmed",
            "message": "Review the stored channel configuration before continuing."})
    current = await channel_configuration_snapshot(db, agent_id=uuid.UUID(descriptor["target_agent_id"]),
        channel_type=descriptor["channel_type"])
    if current != ready.get("configuration"):
        raise HTTPException(status_code=409, detail={"code": "channel_configuration_changed",
            "message": "Channel configuration changed. Review the current settings before continuing."})
