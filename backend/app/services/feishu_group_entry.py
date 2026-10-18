"""Explicit per-installation Feishu group entry, independent of private history."""

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.channel_config import ChannelConfig
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.chat_session import ChatSession
from app.models.tenant_channel_config import TenantChannelConfig
from app.services.channel_ingress_inbox import channel_installation_ref
from app.services.channel_ingress_context import current_channel_ingress_context
from app.services.channel_session import find_or_create_shared_group_session, _archived_external_conv_id

ENTRY_KEY = "shared_group_entry"


def group_entry_settings(config):
    stored = (config.extra_config or {}).get(ENTRY_KEY) or {}
    ids = stored.get("chat_ids", []) if stored.get("app_id") == config.app_id else []
    installation = channel_installation_ref(config, fallback="")
    version = [str(config.id), config.app_id, stored]
    if isinstance(config, TenantChannelConfig):
        version.append(installation)
    revision = hashlib.sha256(json.dumps(version, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if isinstance(config, TenantChannelConfig):
        routes = stored.get("routes", []) if stored.get("installation_ref") == installation else []
        return {"installation_ref": installation, "revision": revision, "routes": routes}
    return {"installation_ref": installation, "revision": revision, "chat_ids": ids}


async def select_shared_feishu_session(db, *, config, agent_id, tenant_id, actor,
    chat_type, chat_id, title, new_session=False):
    if chat_type != "group" or not chat_id:
        return None
    enterprise = isinstance(config, TenantChannelConfig)
    if enterprise:
        live = await db.scalar(select(TenantChannelConfig).where(TenantChannelConfig.id == config.id,
            TenantChannelConfig.tenant_id == tenant_id, TenantChannelConfig.channel_type == "feishu",
            TenantChannelConfig.is_active.is_(True)).execution_options(populate_existing=True))
    else:
        live = await db.scalar(select(ChannelConfig).where(ChannelConfig.id == config.id,
            ChannelConfig.agent_id == agent_id, ChannelConfig.tenant_id == tenant_id,
            ChannelConfig.channel_type == "feishu", ChannelConfig.is_configured.is_(True))
            .execution_options(populate_existing=True))
    if live is None:
        raise HTTPException(409, detail={"code": "shared_group_installation_changed"})
    installation = channel_installation_ref(live, fallback="")
    ingress = current_channel_ingress_context()
    event = await db.get(ChannelIngressEvent, ingress.event_id) if ingress is not None else None
    prior = await db.get(ChatSession, event.result_session_id) if event and event.result_session_id else None
    if prior is not None:
        if (prior.tenant_id != tenant_id or prior.agent_id != agent_id
            or event.installation_ref != installation or event.external_principal_id != actor.external_principal_id):
            raise PermissionError("shared_group_entry_receipt_mismatch")
        if prior.audience_kind != "group":
            return None
        if prior.group_installation_ref != installation or prior.group_subject_id != chat_id:
            raise PermissionError("shared_group_entry_receipt_mismatch")
        session = prior
    elif (any(row["chat_id"] == chat_id and row["agent_id"] == str(agent_id)
        for row in group_entry_settings(live)["routes"]) if enterprise else chat_id in group_entry_settings(live)["chat_ids"]):
        session = await find_or_create_shared_group_session(db, agent_id=agent_id, tenant_id=tenant_id,
            installation_ref=installation, chat_id=chat_id, first_message_title=title)
    else:
        return None
    from app.services.shared_group_authority import resolve_shared_group_entry_authority

    await resolve_shared_group_entry_authority(db, actor=actor, session=session)
    if new_session and prior is None:
        session.external_conv_id = _archived_external_conv_id(session.external_conv_id, session.id)
        await db.flush()
        session = await find_or_create_shared_group_session(db, agent_id=agent_id, tenant_id=tenant_id,
            installation_ref=installation, chat_id=chat_id, first_message_title="New Session")
    if enterprise:
        session.delivery_target_json = {**(session.delivery_target_json or {}), "tenant_channel_config_id": str(live.id),
            "installation_ref": installation, "session_id": str(session.id)}
    event.result_session_id = session.id
    event.external_principal_id = actor.external_principal_id
    ingress.session_id = session.id
    return session


async def save_group_entry_settings(db, *, config, expected_revision, chat_ids):
    if group_entry_settings(config)["revision"] != expected_revision:
        raise HTTPException(409, detail={"code": "shared_group_settings_changed", "next_action": "reload"})
    from app.services.feishu_service import FeishuService

    old_ids = set(group_entry_settings(config)["chat_ids"])
    for chat_id in sorted(set(chat_ids) - old_ids):
        try:
            await FeishuService().get_chat_member_open_ids(config.app_id, config.app_secret, chat_id,
                extra_config=config.extra_config)
        except Exception as exc:
            raise HTTPException(503, detail={"code": "group_membership_unavailable", "retryable": True}) from exc
    config.extra_config = {**(config.extra_config or {}), ENTRY_KEY: {
        "app_id": config.app_id, "chat_ids": sorted(set(chat_ids)), "revision": uuid.uuid4().hex}}
    await db.flush()
    return group_entry_settings(config)


async def save_tenant_group_routes(db, *, config, expected_revision, routes, user):
    from app.core.permissions import require_agent_manage_access
    from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason
    from app.services.feishu_service import FeishuService

    current = group_entry_settings(config)
    if current["revision"] != expected_revision:
        raise HTTPException(409, detail={"code": "shared_group_settings_changed", "next_action": "reload"})
    old = {row["chat_id"]: row["agent_id"] for row in current["routes"]}
    for row in routes:
        agent = await require_agent_manage_access(db, user, uuid.UUID(row["agent_id"]))
        if agent.tenant_id != config.tenant_id or get_agent_lifecycle_block_reason(agent) is not None:
            raise HTTPException(409, detail={"code": "shared_group_employee_unavailable"})
        if old.get(row["chat_id"]) != row["agent_id"]:
            try:
                await FeishuService().get_chat_member_open_ids(config.app_id, config.app_secret, row["chat_id"],
                    extra_config=config.extra_config)
            except Exception as exc:
                raise HTTPException(503, detail={"code": "group_membership_unavailable", "retryable": True}) from exc
    config.extra_config = {**(config.extra_config or {}), ENTRY_KEY: {"app_id": config.app_id,
        "installation_ref": channel_installation_ref(config, fallback=""), "routes": sorted(routes, key=lambda row: row["chat_id"]),
        "revision": uuid.uuid4().hex}}
    await db.flush()
    return group_entry_settings(config)
