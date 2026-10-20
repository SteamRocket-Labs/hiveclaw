"""Original-principal admission and packet-backed channel delivery for remote work."""

import uuid

from fastapi import HTTPException

from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.external_principal import ExternalPrincipal
from app.models.session_v2 import SessionCommand, SessionTurnInput
from app.services.channel_ingress_context import current_channel_ingress_context
from app.services.session_v2_persistence import resolve_session_command_authority, resolve_session_mutation_authority


def _denied():
    return HTTPException(409, detail={'code': 'remote_a2a_channel_authority_unavailable'})


async def capture_origin(db, *, agent, user, session, metadata):
    ingress = current_channel_ingress_context()
    principal_id = getattr(user, 'external_principal_id', None)
    if (ingress is None or principal_id is None or not getattr(user, 'authority_bound', False)
        or (ingress.tenant_id, ingress.agent_id, ingress.external_principal_id) != (agent.tenant_id, agent.id, principal_id)
        or metadata.get('channel_ingress_event_id') != str(ingress.event_id)):
        raise _denied()
    try:
        authority = await resolve_session_mutation_authority(db, user=user, agent_id=agent.id,
            session_id=session.id, action='mutate_session_input', audit=False)
        command_id = uuid.UUID(metadata['session_v2_command_id'])
        row = await db.get(SessionTurnInput, ingress.event_id)
        principal = await db.get(ExternalPrincipal, principal_id)
        if (authority.principal_type != 'external_principal' or authority.principal_id != principal_id
            or row is None or row.command_id != command_id or row.session_id != session.id
            or principal is None or principal.installation_ref != ingress.installation_ref
            or principal.provider != ingress.provider):
            raise _denied()
    except (PermissionError, RuntimeError, LookupError, KeyError, ValueError) as exc:
        raise _denied() from exc
    return {'schema': 'hive.remote_channel_origin.v1', 'ingress_event_id': str(ingress.event_id),
        'command_id': str(command_id), 'external_principal_id': str(principal_id),
        'provider': ingress.provider, 'installation_ref': ingress.installation_ref}


async def validate_origin(db, *, task, state, session):
    origin = state.get('channel_origin')
    try:
        if (not isinstance(origin, dict) or origin.get('schema') != 'hive.remote_channel_origin.v1'
            or origin.get('external_principal_id') != str(task.root_external_principal_id)
            or session.external_principal_id != task.root_external_principal_id
            or origin.get('ingress_event_id') != task.metadata_json.get('channel_ingress_event_id')
            or origin.get('command_id') != task.metadata_json.get('session_v2_command_id')):
            raise _denied()
        event = await db.get(ChannelIngressEvent, uuid.UUID(origin['ingress_event_id']), populate_existing=True)
        command = await db.get(SessionCommand, uuid.UUID(origin['command_id']), populate_existing=True)
        row = await db.get(SessionTurnInput, uuid.UUID(origin['ingress_event_id']), populate_existing=True)
        principal = await db.get(ExternalPrincipal, task.root_external_principal_id, populate_existing=True)
        if (event is None or command is None or row is None or principal is None
            or (event.tenant_id, event.agent_id, event.provider, event.installation_ref) != (
                task.tenant_id, task.parent_agent_id, origin['provider'], origin['installation_ref'])
            or event.external_principal_id not in {None, principal.id}
            or (principal.provider, principal.installation_ref, principal.linked_user_id) != (
                origin['provider'], origin['installation_ref'], task.root_user_id)
            or command.namespace != 'human_input' or command.principal_type != 'external_principal'
            or command.principal_id != principal.id or row.command_id != command.id or row.session_id != session.id):
            raise _denied()
        resolved = await resolve_session_command_authority(db, command=command, session=session,
            action='mutate_session_input')
        if not resolved.actor.authority_bound or resolved.actor.id != task.root_user_id:
            raise _denied()
    except (PermissionError, RuntimeError, LookupError, KeyError, ValueError) as exc:
        raise _denied() from exc


def _recovery_text(view, operation):
    unstarted = (view.get('error_code') == 'remote_a2a_authority_unavailable'
        and operation.get('phase') == 'not_started')
    status = '远端任务尚未启动。' if unstarted else '远端运行结果尚未确认。'
    return status + '请在 Hive 原工作中查看状态和可用的恢复操作。'


async def deliver_packet(db, *, task, result, view, parts):
    from app.services.channel_delivery_outbox import enqueue_terminal_delivery_for_task
    from app.services.remote_a2a_chat import reply_text
    from app.services.remote_a2a_tasks import TERMINAL, _digest

    if (task.metadata_json.get('remote_a2a') or {}).get('dispatch_kind') != 'session':
        return
    notice = bool(view.get('error_code'))
    packet = view.get('operation_observation') or {}
    operation = next(op for op in task.metadata_json['remote_a2a']['operations'] if op['id'] == view['latest_operation_id'])
    text = _recovery_text(view, operation) if notice else reply_text(packet)
    if not text:
        return
    terminal = view['status'] in TERMINAL
    await enqueue_terminal_delivery_for_task(db, task=task, content=text, terminal_status=view['status'],
        artifact_parts=[] if notice else parts, delivery_kind='terminal_result' if terminal else 'interactive_prompt',
        prompt_item_id=None if terminal else uuid.uuid5(task.id,
            f"remote-channel:{view['status']}:{view.get('error_code')}:{_digest(packet)}"),
        external_principal_id=task.root_external_principal_id,
        metadata={'source': 'remote_a2a_chat', 'remote_recovery_notice': notice,
            'remote_result': {'source_kind': 'remote_a2a_result', 'source_id': str(result.id), 'source_version': result.sha256}})


async def validate_delivery(db, *, item):
    from app.models.user import User
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.remote_a2a_artifacts import load_packet, load_status_packet
    from app.services.channel_delivery_outbox import ChannelDeliveryPermanentError

    if item.metadata.get('source') != 'remote_a2a_chat':
        return
    user = await db.get(User, item.user_id, populate_existing=True)
    try:
        if item.metadata.get('remote_recovery_notice') is True:
            task, _result, session, view, operation = await load_status_packet(db, item.tenant_id,
                item.metadata.get('remote_result'), user=user)
            if item.text != _recovery_text(view, operation) or item.artifact_ids or item.terminal_status != view['status']:
                raise _denied()
        else:
            task, _result, session, _packet = await load_packet(db, item.tenant_id,
                item.metadata.get('remote_result'), user=user)
        if (task.id, task.parent_agent_id, task.parent_session_id, task.root_user_id) != (
            item.runtime_task_id, item.agent_id, str(item.session_id), item.user_id):
            raise _denied()
        state = task.metadata_json['remote_a2a']
        if state.get('channel_origin') is not None:
            await validate_origin(db, task=task, state=state, session=session)
    except (ContentInputReceiptError, HTTPException) as exc:
        raise ChannelDeliveryPermanentError('The remote result source or original requester is no longer authorized.') from exc
