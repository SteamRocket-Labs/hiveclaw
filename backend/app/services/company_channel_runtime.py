"""Authenticated company messages enter the normal durable Session V2 path."""

import hashlib
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.chat_session import ChatSession
from app.models.tenant_channel_config import TenantChannelConfig
from app.services.channel_ingress_inbox import accept_authenticated_channel_event, channel_installation_ref
from app.services.company_channel_identity import company_channel_ready, company_principal, redeem_company_challenge


async def _authorized_target(db, config, principal, accepted=None):
    from app.core.permissions import check_agent_access, reject_non_executing_agent_access
    from app.services.external_principal_service import load_external_runtime_actor
    from app.services.tenant_feishu import _tenant_member_assistant

    actor = await load_external_runtime_actor(db, tenant_id=config.tenant_id, principal_id=principal.id,
        expected_user_id=principal.linked_user_id)
    if not actor.authority_bound:
        return None, actor
    if accepted is None:
        return await _tenant_member_assistant(db, tenant_id=config.tenant_id, user=actor), actor
    if str((accepted.metadata_json or {}).get('requester_user_id')) != str(actor.id):
        raise PermissionError('company_channel_requester_changed')
    if accepted.result_session_id:
        session = await db.get(ChatSession, accepted.result_session_id)
        if (session is None or session.tenant_id != config.tenant_id or session.agent_id != accepted.agent_id
            or session.user_id != actor.id or session.external_principal_id != principal.id):
            raise PermissionError('company_channel_original_work_changed')
    try:
        agent, access = await check_agent_access(db, actor, accepted.agent_id)
        reject_non_executing_agent_access(access)
    except HTTPException:
        return None, actor
    return agent.id if agent.tenant_id == config.tenant_id else None, actor


async def accept_company_message(db, *, config, subject_id, text, event_id, delivery_target=None, attachment=None):
    """The caller has verified the concrete provider callback or SDK session."""
    if not company_channel_ready(config) or not subject_id:
        raise PermissionError('company_channel_not_configured')
    control = await redeem_company_challenge(db, config=config, subject_id=subject_id, text=text)
    if control is not None:
        return {'routed': False, **control}
    principal = await company_principal(db, config, subject_id)
    if principal is None:
        return {'routed': False, 'state': 'verification_required'}
    if principal.status != 'active':
        return {'routed': False, 'state': 'installation_revoked'}
    installation = channel_installation_ref(config, fallback='')
    previous = await db.scalar(select(ChannelIngressEvent).where(ChannelIngressEvent.tenant_id == config.tenant_id,
        ChannelIngressEvent.provider == config.channel_type, ChannelIngressEvent.installation_ref == installation,
        ChannelIngressEvent.provider_event_id == event_id)) if event_id else None
    target, actor = await _authorized_target(db, config, principal, previous)
    if target is None:
        return {'routed': False, 'state': 'assistant_required' if actor.authority_bound else 'verification_required'}
    receipt = await accept_authenticated_channel_event(db, tenant_id=config.tenant_id, agent_id=target,
        provider=config.channel_type, installation_ref=installation, provider_event_id=event_id,
        handler_key=f'company.{config.channel_type}.message',
        body={'subject_id': subject_id, 'user_text': text, 'delivery_target': dict(delivery_target or {}),
            **({'attachment': dict(attachment)} if attachment else {})},
        metadata={'tenant_channel_config_id': str(config.id), 'requester_user_id': str(actor.id), 'transport': 'company_authenticated'})
    return {'routed': True, 'state': 'accepted', 'event_id': str(receipt.event_id)}


async def process_company_message(db, item, body):
    from app.services.channel_ingress_context import (
        bind_channel_ingress_external_principal, bind_channel_ingress_runtime_result, current_channel_ingress_context,
    )
    from app.services.channel_session import find_or_create_channel_session
    from app.services.session_live_input import submit_live_human_input

    context = current_channel_ingress_context()
    event = await db.get(ChannelIngressEvent, item.id)
    try:
        config_id = UUID(str(item.metadata['tenant_channel_config_id']))
    except (KeyError, ValueError, TypeError):
        raise PermissionError('company_channel_installation_missing') from None
    config = await db.get(TenantChannelConfig, config_id, populate_existing=True)
    if (context is None or context.event_id != item.id or event is None or event.status != 'processing'
        or event.tenant_id != item.tenant_id or event.agent_id != item.agent_id or not company_channel_ready(config)
        or config.tenant_id != item.tenant_id or config.channel_type != item.provider
        or item.handler_key != f'company.{item.provider}.message'
        or item.installation_ref != channel_installation_ref(config, fallback='')):
        raise PermissionError('company_channel_authenticated_ingress_required')
    principal = await company_principal(db, config, str(body.get('subject_id') or ''))
    if principal is None:
        raise PermissionError('company_channel_sender_unverified')
    target, actor = await _authorized_target(db, config, principal, event)
    if target != item.agent_id:
        raise PermissionError('company_channel_original_employee_unavailable')
    agent = await db.get(Agent, target)
    raw_target = dict(body.get('delivery_target') or {})
    delivery_target = {'channel': item.provider, 'tenant_channel_config_id': str(config.id),
        'installation_ref': item.installation_ref, 'external_principal_id': str(principal.id)}
    if item.provider == 'wecom':
        delivery_target['user_id'] = principal.subject_id
        if (config.extra_config or {}).get('wecom_transport') == 'smart_bot':
            if raw_target.get('wecom_transport') != 'smart_bot' or raw_target.get('bot_id') != config.extra_config['wecom_bot_id']:
                raise PermissionError('company_channel_transport_changed')
            delivery_target.update(wecom_transport='smart_bot', bot_id=raw_target['bot_id'],
                response_url=raw_target.get('response_url'), source_event_id=str(item.id))
    else:
        delivery_target.update(sender_staff_id=principal.subject_id,
            conversation_id=str(raw_target.get('conversation_id') or ''), conversation_type='1',
            session_webhook=str(raw_target.get('session_webhook') or ''))
    key = 'company_' + hashlib.sha256(f'{item.installation_ref}\n{principal.subject_id}'.encode()).hexdigest()
    session = await find_or_create_channel_session(db=db, agent_id=agent.id, tenant_id=item.tenant_id,
        user_id=actor.id, external_principal_id=principal.id, external_conv_id=key, source_channel=item.provider,
        first_message_title=str(body.get('user_text') or ''), delivery_target=delivery_target)
    delivery_target['session_id'] = str(session.id)
    session.delivery_target_json = delivery_target
    bind_channel_ingress_external_principal(principal.id)
    attachments = []
    if body.get('attachment'):
        from app.services.company_channel_files import materialize_company_attachment
        from app.services.feishu_uploads import _upload_name

        try:
            attachments.append(await materialize_company_attachment(db, config=config, actor=actor, session=session,
                attachment=body['attachment'], event_id=item.id))
        except HTTPException as exc:
            if exc.status_code not in {409, 413, 422, 503}:
                raise
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            attachments.append({'name': _upload_name(body['attachment'].get('filename') or body['attachment'].get('kind')),
                'state': 'unavailable', 'reason_code': detail.get('code') or 'company_file_unavailable',
                'recovery_action': 'upload_new_message'})
    db.add(ChatMessage(tenant_id=item.tenant_id, agent_id=agent.id, user_id=actor.id,
        external_principal_id=principal.id, role='user', content=str(body.get('user_text') or ''),
        conversation_id=str(session.id), source_ingress_event_id=item.id))
    await db.flush()
    receipt = await submit_live_human_input(db=db, agent=agent, user=actor, session=session,
        content=str(body.get('user_text') or ''), attachments=attachments, source=item.provider, input_id=item.id,
        idempotency_key=f'channel:{item.provider}:ingress:{item.id}',
        runtime_metadata={'source': item.provider, 'channel': item.provider,
            'channel_ingress_event_id': str(item.id), 'budget_interactive': False})
    run = dict(receipt.get('run') or {})
    run_id = run.get('run_id') or receipt.get('target_run_id')
    if run_id:
        bind_channel_ingress_runtime_result(runtime_task_id=UUID(str(run_id)), session_id=session.id)
    return {'status': 'accepted', 'runtime_task_id': run_id, 'session_id': str(session.id), 'input_id': receipt['input_id']}
