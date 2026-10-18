"""Live Feishu group identity for shared input and source readers."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.channel_config import ChannelConfig
from app.models.external_principal import ExternalPrincipal
from app.services.channel_ingress_inbox import channel_installation_ref
from app.services.feishu_service import FeishuService


async def require_current_group_member(db, *, session, user=None, principal=None):
    if (session.audience_kind != "group" or session.source_channel != "feishu"
        or not session.group_installation_ref or not session.group_subject_id
        or session.user_id is not None or session.external_principal_id is not None):
        raise HTTPException(403, detail={"code": "shared_group_identity_mismatch"})
    config = await db.scalar(select(ChannelConfig).where(
        ChannelConfig.tenant_id == session.tenant_id, ChannelConfig.agent_id == session.agent_id,
        ChannelConfig.channel_type == "feishu", ChannelConfig.is_configured.is_(True)))
    if config is None or channel_installation_ref(config, fallback=f"feishu:{session.agent_id}") != session.group_installation_ref:
        raise HTTPException(403, detail={"code": "shared_group_installation_unavailable"})
    query = select(ExternalPrincipal).where(
        ExternalPrincipal.tenant_id == session.tenant_id, ExternalPrincipal.provider == "feishu",
        ExternalPrincipal.channel_config_id == config.id, ExternalPrincipal.installation_ref == session.group_installation_ref,
        ExternalPrincipal.status == "active")
    if principal is not None:
        query = query.where(ExternalPrincipal.id == principal.id)
    elif user is not None and user.is_active and user.tenant_id == session.tenant_id:
        query = query.where(ExternalPrincipal.linked_user_id == user.id,
            ExternalPrincipal.binding_method == "feishu_qr", ExternalPrincipal.binding_verified_at.is_not(None))
    else:
        raise HTTPException(403, detail={"code": "group_membership_required"})
    principals = list(await db.scalars(query))
    try:
        members = await FeishuService().get_chat_member_open_ids(
            config.app_id, config.app_secret, session.group_subject_id, extra_config=config.extra_config)
    except Exception as exc:
        raise HTTPException(503, detail={"code": "group_membership_unavailable", "retryable": True}) from exc
    if not any(row.subject_id in members for row in principals):
        raise HTTPException(403, detail={"code": "group_membership_required"})
    return config


async def resolve_shared_group_input_authority(db, *, actor, session, ingress_event_id):
    from app.core.permissions import check_agent_access, reject_non_executing_agent_access, require_writable_session
    from app.models.audit import ChatMessage
    from app.models.channel_ingress_event import ChannelIngressEvent
    from app.models.user import User
    from app.services.channel_ingress_context import current_channel_ingress_context
    from app.services.external_principal_service import load_external_runtime_actor
    from app.services.session_v2_persistence import AuthenticatedSessionAuthority, _AUTHORITY_SEAL

    principal = await db.get(ExternalPrincipal, actor.external_principal_id)
    event = await db.get(ChannelIngressEvent, uuid.UUID(str(ingress_event_id)))
    message = await db.scalar(select(ChatMessage).where(
        ChatMessage.tenant_id == session.tenant_id, ChatMessage.agent_id == session.agent_id,
        ChatMessage.conversation_id == str(session.id), ChatMessage.role == "user",
        ChatMessage.external_principal_id == actor.external_principal_id,
        ChatMessage.source_ingress_event_id == ingress_event_id))
    payload = (event.payload_json or {}).get("body", {}) if event is not None else {}
    body = payload.get("event", {}) if isinstance(payload, dict) else {}
    body = body if isinstance(body, dict) else {}
    sender = (body.get("sender") or {}).get("sender_id") or {}
    ingress = current_channel_ingress_context()
    claimed = (ingress is not None and event is not None and event.status == "processing"
        and ingress.event_id == event.id and ingress.tenant_id == session.tenant_id
        and ingress.agent_id == session.agent_id and ingress.provider == "feishu"
        and ingress.installation_ref == session.group_installation_ref
        and ingress.external_principal_id == actor.external_principal_id)
    if (principal is None or event is None or message is None
        or event.tenant_id != session.tenant_id or event.agent_id != session.agent_id
        or event.provider != "feishu" or event.handler_key != "feishu.event"
        or event.status not in {"processing", "processed"}
        or event.installation_ref != session.group_installation_ref
        or (event.external_principal_id != principal.id and not (event.external_principal_id is None and claimed))
        or (body.get("message") or {}).get("chat_type") != "group"
        or (body.get("message") or {}).get("chat_id") != session.group_subject_id
        or sender.get("open_id") != principal.subject_id or principal.linked_user_id != message.user_id):
        raise PermissionError("shared_group_ingress_mismatch")
    fresh = await load_external_runtime_actor(db, tenant_id=session.tenant_id,
        principal_id=principal.id, expected_user_id=message.user_id)
    if ((principal.linked_user_id is not None and not fresh.authority_bound)
        or fresh.id != actor.id or fresh.authority_bound != actor.authority_bound or actor.tenant_id != session.tenant_id
        or not actor.is_active):
        raise PermissionError("external_principal_snapshot_mismatch")
    await require_current_group_member(db, session=session, principal=principal)
    if fresh.id is not None:
        _, access = await check_agent_access(db, await db.get(User, fresh.id), session.agent_id)
        reject_non_executing_agent_access(access)
    require_writable_session(session, action="mutate_session_input")
    return AuthenticatedSessionAuthority(tenant_id=session.tenant_id, agent_id=session.agent_id,
        principal_type="external_principal", principal_id=principal.id, session_id=session.id,
        authority_source="shared_group_ingress", action="mutate_session_input", _seal=_AUTHORITY_SEAL)
