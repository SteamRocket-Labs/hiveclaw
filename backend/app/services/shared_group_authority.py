"""Live Feishu group identity for shared input and source readers."""

import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException
from sqlalchemy import select

from app.models.channel_config import ChannelConfig
from app.models.external_principal import ExternalPrincipal
from app.services.channel_ingress_inbox import channel_installation_ref
from app.services.feishu_service import FeishuService

_GROUP_PERMISSION_EXECUTION = ContextVar("group_permission_execution", default=None)


@contextmanager
def group_permission_execution(*, authority, invocation, control_id):
    """Identify one applied permission effect, independently of a worker lease."""
    token = _GROUP_PERMISSION_EXECUTION.set((authority, invocation.id, invocation.run_id, control_id))
    try:
        yield
    finally:
        _GROUP_PERMISSION_EXECUTION.reset(token)


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


async def resolve_shared_group_web_authority(db, *, session, user, action, audit=True):
    from app.core.permissions import (
        _audit_scoped_business_admin_session_access, check_agent_access, is_scoped_business_admin,
        reject_non_executing_agent_access, require_writable_session,
    )
    from app.services.session_v2_persistence import AuthenticatedSessionAuthority, _AUTHORITY_SEAL

    if action not in {"mutate_session_input", "answer_session_question", "cancel_session_run", "respond_tool_permission"}:
        raise HTTPException(403, detail={"code": "shared_group_input_action_required"})
    await require_current_group_member(db, session=session, user=user)
    agent, access = await check_agent_access(db, user, session.agent_id)
    reject_non_executing_agent_access(access)
    require_writable_session(session, action=action)
    if audit and is_scoped_business_admin(user, resource_tenant_id=session.tenant_id):
        await _audit_scoped_business_admin_session_access(db, user=user, agent=agent, session=session, action=action)
    return AuthenticatedSessionAuthority(tenant_id=session.tenant_id, agent_id=session.agent_id,
        principal_type="user", principal_id=user.id, session_id=session.id,
        authority_source="shared_group_web_member", action=action, _seal=_AUTHORITY_SEAL)


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


def shared_group_command_channel(command, *, session):
    """Use the accepted input's entry facts, independently of a cached socket."""
    if (command is None or command.tenant_id != session.tenant_id or command.session_id != session.id
        or command.namespace != "human_input"):
        raise PermissionError("shared_group_channel_origin_mismatch")
    admitted = dict((command.target_json or {}).get("runtime_metadata") or {})
    channel = "web" if admitted.get("source") in {"web", "session_input_api"} else (
        admitted.get("channel") or session.source_channel)
    if not isinstance(channel, str) or not channel.strip():
        raise PermissionError("shared_group_channel_unavailable")
    return channel


async def load_shared_group_runtime_actor(db, *, task, session):
    """Require a live worker claim or one exact applied permission effect."""
    from datetime import datetime, timezone

    claimed = (task is not None and task.status in {"pending", "running"} and task.claimed_by is not None
        and task.claim_expires_at is not None and task.claim_expires_at > datetime.now(timezone.utc))
    if not claimed and not await _permission_execution_matches(db, task=task, session=session):
        raise PermissionError("shared_group_runtime_origin_mismatch")
    return await _load_shared_group_task_actor(db, task=task, session=session)


async def _permission_execution_matches(db, *, task, session):
    from app.models.session_v2 import SessionCommand, SessionControlInput, SessionToolInvocation
    from app.services.session_model_inputs import _hash

    scope = _GROUP_PERMISSION_EXECUTION.get()
    if scope is None or task is None or task.status not in {"running", "suspended", "resumable"}:
        return False
    authority, invocation_id, run_id, control_id = scope
    if (task.id != run_id or authority.session_id != session.id or authority.tenant_id != session.tenant_id
        or authority.agent_id != session.agent_id or authority.principal_type != "user"
        or authority.action != "respond_tool_permission" or task.root_user_id != authority.principal_id):
        return False
    control = await db.get(SessionControlInput, control_id)
    command = await db.get(SessionCommand, control.command_id) if control else None
    invocation = await db.get(SessionToolInvocation, invocation_id)
    return bool(control and command and invocation and control.tenant_id == session.tenant_id
        and control.session_id == session.id and control.expected_run_id == task.id
        and control.kind == "permission_response" and control.status == "applied"
        and command.tenant_id == session.tenant_id and command.session_id == session.id
        and command.principal_type == "user" and command.principal_id == authority.principal_id
        and command.command_kind == "permission_response" and command.status == "applied"
        and command.request_hash == _hash({"command_kind": command.command_kind, "request": command.request_json})
        and command.target_hash == _hash(command.target_json)
        and (command.target_json or {}).get("invocation_id") == str(invocation.id)
        and control.response_payload_hash == _hash(control.response_payload_json)
        and (control.response_payload_json or {}).get("decision") in {"allow_once", "allow_session"}
        and invocation.tenant_id == session.tenant_id and invocation.session_id == session.id
        and invocation.run_id == task.id and invocation.permission_item_id == control.request_item_id
        and invocation.permission_request_version == control.request_version
        and invocation.permission_authority_snapshot_hash == control.authority_snapshot_hash
        and invocation.permission_receipt_ref == f"session-control:{control.id}:applied"
        and invocation.permission_state == "approved")


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
        or str(metadata.get("external_principal_id") or "") != str(getattr(actor, "external_principal_id", None) or "")):
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
    except HTTPException as exc:
        if exc.status_code >= 500:
            raise
        return False
    except (PermissionError, ValueError, RuntimeError, LookupError):
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


async def bind_shared_group_permission_source(db, *, task, session, invocation):
    from app.models.session_v2 import SessionCommand
    from app.services.delegation_context_sources import PermissionContextSource, _permission_inputs

    metadata = invocation.session_context.metadata
    metadata.pop("_session_permission_context_source", None)
    await load_shared_group_runtime_actor(db, task=task, session=session)
    command = await db.get(SessionCommand, uuid.UUID(task.metadata_json["session_v2_command_id"]))
    admitted = dict((command.target_json or {}).get("runtime_metadata") or {})
    frames = (admitted, group_permission_metadata(runtime_metadata=admitted,
        session_metadata=session.transcript_metadata_json))
    for frame in frames:
        profile = frame.get("permission_profile") or {}
        exact = (profile.get("capability_policy_snapshot") or {}).get("session_exact_scope") is True
        source = PermissionContextSource(
            reference={"source_kind": "session_command", "source_id": str(command.id), "source_version": command.request_hash},
            permission_inputs=_permission_inputs(frame),
            allowed_tool_names=tuple(profile.get("allowed_tools") or ()) if exact else ())
        if source.matches(metadata, invocation.allowed_tool_names):
            metadata["_session_permission_context_source"] = source
            return


def group_permission_metadata(*, runtime_metadata, session_metadata):
    """Keep grant caches at the governed tool boundary, outside model recovery state."""
    from app.services.web_chat_runtime import _merge_runtime_permission_metadata

    def without_grants(value):
        result = dict(value or {})
        result.pop("session_permission_grants", None)
        if isinstance(result.get("permission_profile"), dict):
            result["permission_profile"] = dict(result["permission_profile"])
            result["permission_profile"].pop("session_grants", None)
        return result

    metadata = _merge_runtime_permission_metadata(runtime_metadata=without_grants(runtime_metadata),
        session_metadata=without_grants(session_metadata))
    profile = dict(metadata.get("permission_profile") or {})
    profile["session_grants"] = []
    profile["capability_policy_snapshot"] = {**(profile.get("capability_policy_snapshot") or {}), "shared_group_session": True}
    metadata["permission_profile"] = profile
    return metadata


async def apply_group_permission_grants(context):
    from dataclasses import replace
    from app.database import tenant_scoped_session
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionControlInput

    profile = context.permission_profile
    snapshot = profile.get("capability_policy_snapshot") if isinstance(profile, dict) else getattr(profile, "capability_policy_snapshot", None)
    if not (snapshot or {}).get("shared_group_session"):
        return
    if isinstance(profile, dict):
        from app.runtime.ccplus_contracts import build_permission_profile

        profile = build_permission_profile(profile)
    async with tenant_scoped_session(uuid.UUID(str(context.tenant_id))) as db:
        session = await db.get(ChatSession, uuid.UUID(str(context.session_id)))
        if session is None or session.agent_id != context.agent_id or session.audience_kind != "group":
            raise PermissionError("shared_group_permission_scope_mismatch")
        actor = await load_shared_group_runtime_actor(db, task=await db.get(RuntimeTask, uuid.UUID(str(context.runtime_task_id))), session=session)
        if actor.id != context.user_id:
            raise PermissionError("shared_group_permission_requester_mismatch")
        grants = []
        for grant in (session.transcript_metadata_json or {}).get("session_permission_grants", []):
            if not isinstance(grant, dict) or grant.get("principal_type") != "user" or grant.get("principal_id") != str(actor.id):
                continue
            control = await db.scalar(select(SessionControlInput).join(SessionCommand, SessionCommand.id == SessionControlInput.command_id)
                .where(SessionControlInput.id == uuid.UUID(grant["control_id"]), SessionControlInput.tenant_id == session.tenant_id,
                    SessionControlInput.session_id == session.id, SessionControlInput.kind == "permission_response",
                    SessionControlInput.status == "applied", SessionControlInput.authority_snapshot_hash == grant["authority_snapshot_hash"],
                    SessionCommand.principal_type == "user", SessionCommand.principal_id == actor.id,
                    SessionCommand.status == "applied"))
            if control is not None and control.response_payload_json == {"decision": "allow_session"}:
                grants.append(dict(grant))
        context.permission_profile = replace(profile, session_grants=tuple(grants))


async def authorize_group_tool_permission(db, *, authority, invocation):
    """Recheck the original requester and current sources before a group effect."""
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.user import User
    from app.services.shared_group_history import authorize_group_history_reader, group_event_access

    session = await db.get(ChatSession, authority.session_id)
    if session is None or session.audience_kind != "group":
        return
    if authority.principal_type != "user" or authority.action != "respond_tool_permission":
        raise HTTPException(403, detail={"code": "shared_group_permission_requester_required"})
    if not await shared_group_input_matches_run(db, session=session, authority=authority, run_id=invocation.run_id):
        raise HTTPException(409, detail={"code": "shared_group_requester_conflict"})
    user = await db.scalar(select(User).where(User.id == authority.principal_id, User.tenant_id == authority.tenant_id,
        User.is_active.is_(True)).execution_options(populate_existing=True))
    if user is None:
        raise HTTPException(403, detail={"code": "group_membership_required"})
    await authorize_group_history_reader(db, session=session, user=user, audit=False)
    event = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.tenant_id == authority.tenant_id,
        ChatTranscriptEvent.agent_id == authority.agent_id, ChatTranscriptEvent.session_id == authority.session_id,
        ChatTranscriptEvent.invocation_id == invocation.id, ChatTranscriptEvent.item_id == invocation.permission_item_id,
        ChatTranscriptEvent.item_kind == "tool_permission", ChatTranscriptEvent.lifecycle == "waiting")
        .order_by(ChatTranscriptEvent.sequence).limit(1))
    if event is None:
        raise HTTPException(503, detail={"code": "group_permission_source_unavailable", "retryable": True})
    receipt = (await group_event_access(db, session=session, events=[event], user=user))[event.id]
    if receipt is not None:
        raise HTTPException(403 if receipt["status"] == "denied" else 503,
            detail={"code": receipt["code"], "source_access_receipt": receipt})


async def read_shared_group_history(db, *, session, messages, user=None, run_id=None):
    """Keep immutable protocol groups only when this reader can use their full sources."""
    from app.models.runtime_task import RuntimeTask
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.session_model_inputs import _origin_dependencies, _unique_dependencies

    principal = None
    if run_id is not None:
        actor = await load_shared_group_runtime_actor(db, task=await db.get(RuntimeTask, run_id), session=session)
        principal = AgentRuntimePrincipal(agent_id=session.agent_id, requester_user_id=actor.id,
            session_id=str(session.id), runtime_task_id=str(run_id))
    else:
        from app.core.permissions import check_agent_access, is_scoped_business_admin

        if user is None:
            raise HTTPException(403, detail={"code": "group_reader_required"})
        await check_agent_access(db, user, session.agent_id)
        if not is_scoped_business_admin(user, resource_tenant_id=session.tenant_id):
            await require_current_group_member(db, session=session, user=user)
    groups = {}
    for message in messages:
        groups.setdefault(message.group_id, []).append(message)
    allowed, held = set(), []
    for group_id, group in groups.items():
        dependencies, complete = [], True
        for message in group:
            sources, bound = await _origin_dependencies(db, tenant_id=session.tenant_id, agent_id=session.agent_id, ref=message.id)
            dependencies.extend(sources)
            complete = complete and bound
        try:
            if not complete:
                raise ContentInputReceiptError("group_history_sources_unavailable", "The prior source inventory is incomplete.", 503)
            await _validate_input_dependencies(db, session.tenant_id, _unique_dependencies(dependencies),
                user=user, runtime_request=principal)
        except (ContentInputReceiptError, HTTPException) as exc:
            denied = exc.status_code == 403
            held.append({"kind": "source_access_denied" if denied else "source_unavailable",
                "code": getattr(exc, "code", "group_history_sources_unavailable"), "retryable": not denied,
                "evidence_refs": [message.id for message in group]})
            continue
        allowed.add(group_id)
    return [message for message in messages if message.group_id in allowed], held


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
