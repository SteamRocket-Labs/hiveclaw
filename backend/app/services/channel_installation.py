"""Resolve an explicitly recorded enterprise reply without an employee-bot fallback."""

import uuid

from sqlalchemy import select

from app.models.agent import Agent
from app.models.external_principal import ExternalPrincipal
from app.models.tenant_channel_config import TenantChannelConfig


async def load_tenant_channel_for_target(db, *, agent_id, channel, target, expected_config_id=None):
    from app.services.channel_ingress_inbox import channel_installation_ref
    from app.services.external_principal_service import load_external_runtime_actor

    try:
        config_id = uuid.UUID(str(target["tenant_channel_config_id"]))
        principal_id = uuid.UUID(str(target["external_principal_id"])) if target.get("external_principal_id") else None
    except (KeyError, TypeError, ValueError):
        return None
    if channel not in {"feishu", "dingtalk", "wecom"} or expected_config_id is not None and config_id != expected_config_id:
        return None
    config = await db.scalar(select(TenantChannelConfig).join(Agent, Agent.tenant_id == TenantChannelConfig.tenant_id)
        .where(Agent.id == agent_id, TenantChannelConfig.id == config_id, TenantChannelConfig.channel_type == channel,
            TenantChannelConfig.is_active.is_(True)).execution_options(populate_existing=True))
    if config is not None and target.get("chat_type") == "group" and principal_id is None:
        from app.models.chat_session import ChatSession

        try:
            session_id = uuid.UUID(str(target["session_id"]))
        except (KeyError, ValueError, TypeError):
            return None
        session = await db.get(ChatSession, session_id)
        return config if (session is not None and session.tenant_id == config.tenant_id and session.agent_id == agent_id
            and session.audience_kind == "group" and session.source_channel == "feishu" and session.user_id is None
            and session.external_principal_id is None and session.group_installation_ref == target.get("installation_ref")
            and session.group_installation_ref == channel_installation_ref(config, fallback="")
            and target.get("receive_id_type") == "chat_id" and target.get("receive_id") == session.group_subject_id
            and target.get("chat_id") == session.group_subject_id) else None
    if principal_id is None:
        return None
    principal = await db.get(ExternalPrincipal, principal_id, populate_existing=True)
    if (config is None or principal is None or principal.status != "active" or principal.provider != channel
        or principal.tenant_id != config.tenant_id or principal.tenant_channel_config_id != config.id
        or principal.channel_config_id is not None or principal.installation_ref != target.get("installation_ref")
        or principal.installation_ref != channel_installation_ref(config, fallback="")):
        return None
    actor = await load_external_runtime_actor(db, tenant_id=config.tenant_id,
        principal_id=principal.id, expected_user_id=principal.linked_user_id)
    if channel in {"dingtalk", "wecom"}:
        from app.services.company_channel_identity import company_channel_ready
        recipient = target.get("user_id") if channel == "wecom" else target.get("sender_staff_id")
        if not company_channel_ready(config) or recipient != principal.subject_id:
            return None
        if channel == "dingtalk" and target.get("conversation_type") != "1":
            return None
    return config if actor.authority_bound else None
