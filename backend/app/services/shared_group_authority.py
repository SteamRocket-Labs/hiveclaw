"""Live Feishu group identity for shared input and source readers."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.channel_config import ChannelConfig
from app.models.external_principal import ExternalPrincipal
from app.services.channel_ingress_inbox import channel_installation_ref
from app.services.feishu_service import FeishuService


async def load_current_group_members(db, *, session):
    if (session.audience_kind != "group" or session.source_channel != "feishu"
        or not session.group_installation_ref or not session.group_subject_id
        or session.user_id is not None or session.external_principal_id is not None):
        raise HTTPException(403, detail={"code": "shared_group_identity_mismatch"})
    config = await db.scalar(select(ChannelConfig).where(
        ChannelConfig.tenant_id == session.tenant_id, ChannelConfig.agent_id == session.agent_id,
        ChannelConfig.channel_type == "feishu", ChannelConfig.is_configured.is_(True)))
    if config is None or channel_installation_ref(config, fallback=f"feishu:{session.agent_id}") != session.group_installation_ref:
        raise HTTPException(403, detail={"code": "shared_group_installation_unavailable"})
    try:
        members = await FeishuService().get_chat_member_open_ids(
            config.app_id, config.app_secret, session.group_subject_id, extra_config=config.extra_config)
    except Exception as exc:
        raise HTTPException(503, detail={"code": "group_membership_unavailable", "retryable": True}) from exc
    return config, members


async def require_current_group_member(db, *, session, user=None, principal=None):
    config, members = await load_current_group_members(db, session=session)
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


async def load_shared_group_runtime_actor(db, *, task, session):
    """Require a live execution claim as well as the canonical requester."""
    from datetime import datetime, timezone

    if (task is None or task.status not in {"pending", "running"} or task.claimed_by is None
        or task.claim_expires_at is None or task.claim_expires_at <= datetime.now(timezone.utc)):
        raise PermissionError("shared_group_runtime_origin_mismatch")
    return await _load_shared_group_task_actor(db, task=task, session=session)


async def _load_shared_group_task_actor(db, *, task, session):
    """Rebuild request identity from immutable inputs, independently of a worker claim."""
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.services.session_model_inputs import _hash
    from app.services.session_v2_persistence import resolve_session_command_authority
    from app.services.web_chat_runtime import EXECUTABLE_CHAT_TASK_TYPES

    metadata = task.metadata_json or {}
    try:
        input_id = uuid.UUID(metadata.get("session_v2_input_id") or metadata["session_v2_rolled_over_input_id"])
        command_id = uuid.UUID(metadata["session_v2_command_id"])
    except (ValueError, TypeError, KeyError) as exc:
        raise PermissionError("shared_group_runtime_origin_unavailable") from exc
    row = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.id == input_id,
        SessionTurnInput.command_id == command_id, SessionTurnInput.tenant_id == session.tenant_id,
        SessionTurnInput.session_id == session.id))
    command = await db.get(SessionCommand, command_id)
    if (session.audience_kind != "group" or task.tenant_id != session.tenant_id
        or task.parent_agent_id != session.agent_id or task.parent_session_id != str(session.id)
        or task.task_type not in EXECUTABLE_CHAT_TASK_TYPES
        or row is None or command is None or row.target_run_id not in {None, task.id}
        or task.id not in {uuid.uuid5(input_id, "session-v2-runtime-run"), uuid.uuid5(input_id, "session-v2-successor-run")}
        or command.request_hash != _hash({"command_kind": command.command_kind, "request": command.request_json})
        or command.target_hash != _hash(command.target_json)):
        raise PermissionError("shared_group_runtime_origin_mismatch")
    context = await resolve_session_command_authority(db, command=command, session=session, action="mutate_session_input")
    actor = context.actor
    if (task.root_user_id != actor.id or str(metadata.get("user_id") or "") != str(actor.id or "")
        or str(metadata.get("external_principal_id") or "") != str(actor.external_principal_id)):
        raise PermissionError("shared_group_runtime_actor_mismatch")
    return actor


async def shared_group_input_matches_run(db, *, session, authority, run_id):
    from app.models.runtime_task import RuntimeTask
    from app.services.external_principal_service import load_external_runtime_actor

    if (authority.tenant_id != session.tenant_id or authority.agent_id != session.agent_id
        or authority.session_id != session.id or run_id is None):
        return False
    try:
        task = await db.get(RuntimeTask, uuid.UUID(str(run_id)))
        if task is None:
            return False
        original = await _load_shared_group_task_actor(db, task=task, session=session)
        if authority.principal_type == "user":
            return original.id is not None and original.id == authority.principal_id
        principal = await db.scalar(select(ExternalPrincipal).where(ExternalPrincipal.id == authority.principal_id,
            ExternalPrincipal.tenant_id == session.tenant_id, ExternalPrincipal.installation_ref == session.group_installation_ref,
            ExternalPrincipal.status == "active").execution_options(populate_existing=True))
        if principal is None:
            return False
        incoming = await load_external_runtime_actor(db, tenant_id=session.tenant_id, principal_id=principal.id,
            expected_user_id=principal.linked_user_id)
        if original.id is not None:
            return original.id == incoming.id
        return incoming.id is None and original.external_principal_id == incoming.external_principal_id
    except (HTTPException, PermissionError, ValueError, RuntimeError, LookupError):
        return False


async def validate_shared_group_model_inputs(db, *, tenant_id, agent_id, session_id, run_id, input_bindings):
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.personal_knowledge_access import AgentRuntimePrincipal

    session = await db.scalar(select(ChatSession).where(ChatSession.id == session_id,
        ChatSession.tenant_id == tenant_id, ChatSession.agent_id == agent_id))
    if session is None or session.audience_kind != "group":
        return
    task = await db.get(RuntimeTask, run_id)
    actor = await load_shared_group_runtime_actor(db, task=task, session=session) if task is not None else None
    if actor is None or actor.id is None or not (input_bindings.get("dependency_coverage") or {}).get("complete"):
        raise ContentInputReceiptError("group_model_sources_unavailable", "The shared turn lacks its complete authorized source frame.", 503)
    principal = AgentRuntimePrincipal(agent_id=agent_id, requester_user_id=actor.id, session_id=str(session_id),
        runtime_task_id=str(run_id))
    await _validate_input_dependencies(db, tenant_id, input_bindings.get("dependencies"), user=None, runtime_request=principal)


async def validate_shared_group_delivery(db, *, item, session):
    """Release canonical output only to current recipients who can read every source."""
    from app.models.session_v2 import SessionModelResult
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.user import User
    from app.services.channel_delivery_outbox import ChannelDeliveryPermanentError, ChannelDeliveryRetryableError
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.session_model_inputs import _model_dependencies

    target = item.delivery_target
    if (item.tenant_id != session.tenant_id or item.agent_id != session.agent_id
        or target.get("channel") != "feishu" or target.get("receive_id_type") != "chat_id"
        or target.get("receive_id") != session.group_subject_id or target.get("chat_id") != session.group_subject_id):
        raise ChannelDeliveryPermanentError("shared_group_delivery_target_mismatch")
    try:
        config, members = await load_current_group_members(db, session=session)
        if item.channel_config_id != config.id or not members:
            raise HTTPException(503, detail={"code": "group_membership_unavailable"})
        principals = list(await db.scalars(select(ExternalPrincipal).where(
            ExternalPrincipal.tenant_id == session.tenant_id, ExternalPrincipal.provider == "feishu",
            ExternalPrincipal.installation_ref == session.group_installation_ref, ExternalPrincipal.channel_config_id == config.id,
            ExternalPrincipal.subject_id.in_(members), ExternalPrincipal.status == "active",
            ExternalPrincipal.linked_user_id.is_not(None), ExternalPrincipal.binding_method == "feishu_qr",
            ExternalPrincipal.binding_verified_at.is_not(None))))
        if {row.subject_id for row in principals} != members:
            raise HTTPException(503, detail={"code": "group_recipient_identity_unavailable"})
        result = await db.scalar(select(SessionModelResult).join(ChatTranscriptEvent,
            ChatTranscriptEvent.id == SessionModelResult.round_committed_event_id).where(
            SessionModelResult.tenant_id == session.tenant_id, ChatTranscriptEvent.tenant_id == session.tenant_id,
            ChatTranscriptEvent.agent_id == session.agent_id, ChatTranscriptEvent.session_id == session.id,
            ChatTranscriptEvent.run_id == item.runtime_task_id,
            SessionModelResult.session_id == session.id, SessionModelResult.run_id == item.runtime_task_id,
            SessionModelResult.state == "round_committed").order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
        dependencies, complete = _model_dependencies(result)
        if not complete:
            raise ContentInputReceiptError("group_output_sources_unavailable", "The group output lacks its complete source receipt.", 503)
        for user_id in {row.linked_user_id for row in principals}:
            user = await db.scalar(select(User).where(User.id == user_id, User.tenant_id == session.tenant_id,
                User.is_active.is_(True)).execution_options(populate_existing=True))
            if user is None:
                raise HTTPException(503, detail={"code": "group_recipient_identity_unavailable"})
            await _validate_input_dependencies(db, session.tenant_id, dependencies, user=user)
    except (HTTPException, ContentInputReceiptError) as exc:
        detail = getattr(exc, "detail", None)
        code = getattr(exc, "code", None) or (detail.get("code") if isinstance(detail, dict) else None)
        message = f"shared_group_output_unavailable:{code or 'source_access_denied'}"
        if exc.status_code == 403:
            raise ChannelDeliveryPermanentError(message) from exc
        raise ChannelDeliveryRetryableError(message) from exc
