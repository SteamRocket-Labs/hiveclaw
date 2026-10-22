"""Recover a committed approval decision and update only its confirmed card."""

import json
import uuid

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from app.models.agent import Agent
from app.models.audit import ApprovalRequest
from app.models.channel_config import ChannelConfig
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.external_principal import ExternalPrincipal
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from app.services.channel_ingress_context import current_channel_ingress_context
from app.services.channel_ingress_inbox import channel_installation_ref
from app.services.channel_provider_receipts import ProviderReceiptError, feishu_message_scope

KEY = 'feishu_card_decision'
SCHEMA = 'hive.feishu_card_decision.v1'
NOTIFICATIONS = 'feishu_approval_cards'


async def load_card_config(db, *, tenant_id, agent_id, source):
    enterprise_id = (source.metadata_json or {}).get('tenant_channel_config_id') if source else None
    if enterprise_id:
        return await db.scalar(select(TenantChannelConfig).where(TenantChannelConfig.id == uuid.UUID(enterprise_id),
            TenantChannelConfig.tenant_id == tenant_id, TenantChannelConfig.channel_type == 'feishu')
            .execution_options(populate_existing=True))
    return await db.scalar(select(ChannelConfig).where(ChannelConfig.agent_id == agent_id,
        ChannelConfig.tenant_id == tenant_id, ChannelConfig.channel_type == 'feishu').execution_options(populate_existing=True))


def record_card_notification(approval, *, config, user, response):
    reference = ((response.get('data') or {}).get('message_id') if isinstance(response, dict) else None)
    confirmed = isinstance(response, dict) and type(response.get('code')) is int and response['code'] == 0
    confirmed = confirmed and isinstance(reference, str) and bool(reference)
    record = {'user_id': str(user.id), 'configuration_id': str(config.id),
        'installation_ref': channel_installation_ref(config, fallback=''), 'scope': feishu_message_scope(config),
        'status': 'sent' if confirmed else 'unknown', 'provider_reference': reference if confirmed else None}
    receipt = dict(approval.execution_receipt or {})
    receipt[NOTIFICATIONS] = [*(receipt.get(NOTIFICATIONS) or []), record]
    approval.execution_receipt = receipt


def card_response(approval, user, action):
    status_emoji = '✅' if approval.status == 'approved' else '❌'
    status_text = '已批准' if approval.status == 'approved' else '已拒绝'
    expected_status = 'approved' if action == 'approve' else 'rejected'
    return {'toast': {'type': 'success' if approval.status == expected_status else 'error', 'content': status_text},
        'card': {'config': {'wide_screen_mode': True},
            'header': {'template': 'green' if approval.status == 'approved' else 'red',
                'title': {'tag': 'plain_text', 'content': f'{status_emoji} 审批已处理'}},
            'elements': [{'tag': 'div', 'text': {'tag': 'lark_md',
                'content': f'**操作**: {approval.action_type}\n**结果**: {status_text}\n**处理人**: {user.display_name}'}}]}}


async def record_card_decision(db, *, approval, user, principal):
    ingress = current_channel_ingress_context()
    if ingress is None:
        return
    row = await db.scalar(select(ChannelIngressEvent).where(ChannelIngressEvent.id == ingress.event_id,
        ChannelIngressEvent.tenant_id == approval.tenant_id, ChannelIngressEvent.agent_id == approval.agent_id)
        .with_for_update())
    if row is None or row.status != 'processing':
        raise HTTPException(403, detail={'code': 'feishu_card_origin_changed'})
    row.metadata_json = {**row.metadata_json, KEY: {'schema': SCHEMA, 'approval_id': str(approval.id),
        'actor_user_id': str(user.id), 'principal_id': str(principal.id), 'outcome': approval.status,
        'resolved_by': str(approval.resolved_by) if approval.resolved_by else None,
        'resolved_at': approval.resolved_at.isoformat(), 'source_payload_digest': row.payload_digest,
        'delivery': {'status': 'pending'}}}


def _unavailable(reason, *, status='unavailable'):
    return {'status': status, 'reason_code': reason, 'retryable': False,
        'recovery_action': 'continue_in_original_web_work'}


async def resume_card_decision(db, *, item):
    """Resume projection without executing the already committed decision again."""
    from app.api.feishu import _authenticate_card_callback, parse_feishu_card_action
    from app.services.approval_service import can_resolve_approval
    from app.services.external_principal_service import load_external_runtime_actor
    from app.services.feishu_platform import FeishuAPIError
    from app.services.feishu_service import feishu_service

    row = await db.scalar(select(ChannelIngressEvent).where(ChannelIngressEvent.id == item.id,
        ChannelIngressEvent.tenant_id == item.tenant_id, ChannelIngressEvent.agent_id == item.agent_id)
        .with_for_update().execution_options(populate_existing=True))
    saved = dict((row.metadata_json or {}).get(KEY) or {}) if row else {}
    if not saved:
        return None

    def result(delivery):
        return {'status': 'processed', 'approval_id': saved.get('approval_id'),
            'approval_status': saved.get('outcome'), 'card_delivery': delivery}

    async def store(delivery):
        row.metadata_json = {**row.metadata_json, KEY: {**saved, 'delivery': delivery}}
        await db.commit()
        return result(delivery)

    if (row.status != 'processing' or row.attempt_count != item.attempt_count or row.payload_digest != item.payload_digest
        or saved.get('schema') != SCHEMA or saved.get('source_payload_digest') != item.payload_digest):
        return result(_unavailable('feishu_card_decision_scope_changed', status='denied'))
    body = item.payload.get('body') or item.payload
    callback = body.get('event') if item.handler_key == 'feishu.event' else body
    config = await load_card_config(db, tenant_id=item.tenant_id, agent_id=item.agent_id, source=row)
    try:
        await _authenticate_card_callback(db, request=None, body=callback, config=config,
            tenant_id=item.tenant_id, agent_id=item.agent_id)
    except HTTPException:
        return await store(_unavailable('feishu_card_origin_changed', status='denied'))
    decision = parse_feishu_card_action(callback)
    if decision is None or decision[0] != saved.get('approval_id'):
        return await store(_unavailable('feishu_card_decision_scope_changed', status='denied'))
    try:
        approval = await db.get(ApprovalRequest, uuid.UUID(saved['approval_id']), populate_existing=True)
        principal = await db.get(ExternalPrincipal, uuid.UUID(saved['principal_id']), populate_existing=True)
        user = await db.get(User, uuid.UUID(saved['actor_user_id']), populate_existing=True)
    except (ValueError, KeyError, TypeError):
        return await store(_unavailable('feishu_card_decision_scope_changed', status='denied'))
    actor = (await load_external_runtime_actor(db, tenant_id=item.tenant_id, principal_id=principal.id,
        expected_user_id=user.id)) if principal is not None and user is not None else None
    agent = await db.get(Agent, item.agent_id, populate_existing=True)
    resolved_by = str(approval.resolved_by) if approval is not None and approval.resolved_by else None
    if (approval is None or approval.tenant_id != item.tenant_id or approval.agent_id != item.agent_id
        or approval.status != saved.get('outcome') or approval.status not in {'approved', 'rejected'}
        or resolved_by != saved.get('resolved_by') or approval.resolved_at is None
        or approval.resolved_at.isoformat() != saved.get('resolved_at') or actor is None or not actor.authority_bound
        or principal.provider != 'feishu' or principal.installation_ref != item.installation_ref
        or principal.subject_id != (callback.get('operator') or {}).get('open_id', callback.get('open_id'))
        or agent is None or not await can_resolve_approval(db, agent=agent, user=user, approval=approval)):
        return await store(_unavailable('feishu_card_actor_or_decision_changed', status='denied'))
    message_id = (callback.get('context') or {}).get('open_message_id') or callback.get('open_message_id')
    notifications = (approval.execution_receipt or {}).get(NOTIFICATIONS) or []
    if not isinstance(message_id, str) or not any(record.get('status') == 'sent'
        and record.get('provider_reference') == message_id and record.get('user_id') == str(user.id)
        and record.get('configuration_id') == str(config.id) and record.get('installation_ref') == item.installation_ref
        and record.get('scope') == feishu_message_scope(config) for record in notifications):
        return await store(_unavailable('feishu_card_message_receipt_unavailable'))
    delivery = saved.get('delivery') or {}
    if delivery.get('status') in {'delivered', 'denied', 'needs_reconciliation', 'failed'} or (
        delivery.get('status') == 'unavailable' and not delivery.get('retryable')):
        return result(delivery)
    if delivery.get('status') == 'sending':
        return await store(_unavailable('feishu_card_update_outcome_unknown', status='needs_reconciliation'))
    attempt_id = uuid.uuid4().hex
    await store({'status': 'sending', 'provider_reference': message_id, 'attempt_id': attempt_id})
    retry = False
    try:
        response = await feishu_service.patch_message(config.app_id, config.app_secret, message_id,
            json.dumps(card_response(approval, user, decision[1])['card'], ensure_ascii=False), extra_config=config.extra_config)
        if not isinstance(response, dict) or type(response.get('code')) is not int or response['code'] != 0:
            delivery = _unavailable('feishu_card_update_receipt_unverified', status='needs_reconciliation')
        else:
            delivery = {'status': 'delivered', 'provider_reference': message_id, 'retryable': False}
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        retry = True
        delivery = {**_unavailable('feishu_card_update_connection_unavailable'), 'retryable': True}
    except FeishuAPIError as exc:
        retry = exc.http_status == 429
        if retry:
            delivery = {**_unavailable('feishu_card_update_rate_limited'), 'retryable': True}
        elif exc.http_status is not None and exc.http_status >= 500:
            delivery = _unavailable('feishu_card_update_outcome_unknown', status='needs_reconciliation')
        elif exc.code is not None or exc.http_status is not None and 400 <= exc.http_status < 500:
            delivery = _unavailable('feishu_card_update_refused', status='failed')
        else:
            delivery = _unavailable('feishu_card_update_outcome_unknown', status='needs_reconciliation')
    except Exception:
        delivery = _unavailable('feishu_card_update_outcome_unknown', status='needs_reconciliation')
    await db.refresh(row, with_for_update=True)
    if (row.status != 'processing' or row.attempt_count != item.attempt_count
        or ((row.metadata_json or {}).get(KEY) or {}).get('delivery', {}).get('attempt_id') != attempt_id):
        return result(_unavailable('feishu_card_update_claim_changed', status='needs_reconciliation'))
    answer = await store(delivery)
    if retry:
        raise ProviderReceiptError('Feishu card update temporarily unavailable', status='unavailable', retryable=True)
    return answer
