"""Recoverable permission rejection replies bound to authenticated ingress."""

import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.channel_ingress_event import ChannelIngressEvent
from app.services.content_input_receipts import ContentInputReceiptError

KEY = 'original_work_permission_feedback'
SCHEMA = 'hive.original_work_permission_feedback.v1'


def _public_receipt(saved):
    return {key: value for key, value in saved.items() if key != 'rejection'}


async def record_permission_reply_rejection(db, *, item, result):
    from app.core.policy import write_audit_event
    from app.database import pin_rls_tenant_context

    await pin_rls_tenant_context(db, item.tenant_id)
    row = await db.scalar(select(ChannelIngressEvent).where(ChannelIngressEvent.id == item.id,
        ChannelIngressEvent.tenant_id == item.tenant_id, ChannelIngressEvent.agent_id == item.agent_id).with_for_update())
    if (row is None or row.status != 'processing' or row.attempt_count != item.attempt_count
        or row.payload_digest != item.payload_digest or row.provider != 'feishu' or row.handler_key != 'feishu.event'):
        return False
    if (row.metadata_json or {}).get(KEY):
        return True
    rejection = {key: result[key] for key in ('permission_status', 'reason_code', '_permission_feedback_notice_id') if key in result}
    audit = await write_audit_event(db, event_type='original_work.permission_reply_feedback', actor_type='channel_ingress',
        actor_id=item.id, tenant_id=item.tenant_id, action='record_permission_rejection', resource_type='channel_ingress_event',
        resource_id=item.id, request_id=item.id, details={'schema': SCHEMA, 'rejection': rejection})
    row.metadata_json = {**(row.metadata_json or {}), KEY: {'schema': SCHEMA, 'status': 'pending',
        'retryable': False, 'audit_event_id': str(audit.event_id), 'rejection': rejection}}
    await db.commit()
    return True


async def resume_permission_reply_rejection(db, *, item):
    from app.core.policy import compute_audit_event_hash
    from app.models.security_audit import SecurityAuditEvent

    metadata = await db.scalar(select(ChannelIngressEvent.metadata_json).where(ChannelIngressEvent.id == item.id,
        ChannelIngressEvent.tenant_id == item.tenant_id, ChannelIngressEvent.agent_id == item.agent_id))
    saved = (metadata or {}).get(KEY)
    if not saved:
        return None
    invalid = {'permission_status': 'needs_reconciliation', 'reason_code': 'permission_feedback_receipt_unverified'}
    try:
        audit = await db.get(SecurityAuditEvent, uuid.UUID(saved['audit_event_id']))
        if (audit is None or audit.tenant_id != item.tenant_id or audit.resource_type != 'channel_ingress_event'
            or audit.resource_id != item.id or audit.request_id != item.id or audit.actor_type != 'channel_ingress'
            or audit.actor_id != item.id or audit.action != 'record_permission_rejection'
            or audit.event_type != 'original_work.permission_reply_feedback'
            or audit.details != {'schema': SCHEMA, 'rejection': saved['rejection']}):
            return invalid
        fields = {key: getattr(audit, key) for key in ('event_type', 'severity', 'actor_type', 'actor_id', 'tenant_id',
            'action', 'resource_type', 'resource_id', 'details', 'ip_address', 'request_id', 'execution_identity_type',
            'execution_identity_id', 'execution_identity_label', 'prev_hash')}
        return saved['rejection'] if compute_audit_event_hash(**fields) == audit.event_hash else invalid
    except (KeyError, ValueError, TypeError):
        return invalid


def feedback_text(result, url):
    if result.get('permission_status') == 'needs_reconciliation':
        text = '无法核验这条引用对应的权限请求。请登录 HiveClaw，在原工作中查看最新状态。'
    elif result.get('reason_code') == 'tool_permission_request_expired':
        text = '这项权限请求已过期。请登录 HiveClaw，在原工作中查看当前状态。'
    elif result.get('reason_code') == 'original_permission_command_required':
        text = '这条权限回复未被应用。请引用权限提示回复 /allow 或 /deny，或登录 HiveClaw 在原工作中处理。'
    else:
        text = '这条权限回复未被应用。请登录 HiveClaw，在原工作中查看最新状态后重试。'
    return text + (f'\n{url}' if url else '')


async def _recovery_entry(db, *, item, result, config, sender, message, scope):
    from app.config import get_settings
    from app.core.permissions import authorize_session_action
    from app.core.resource_authority import authorize_resource_action
    from app.core.source_read_purpose import source_recipient_read
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.external_principal import ExternalPrincipal
    from app.models.runtime_task import RuntimeTask
    from app.services.external_principal_service import load_external_runtime_actor
    from app.services.original_work_permission_notices import _original_work
    from app.services.runtime_user_identity import load_scoped_runtime_user

    base = (get_settings().PUBLIC_BASE_URL or get_settings().BASE_URL).rstrip('/')
    generic = (base + '/' if base else '', None)
    try:
        notice = await db.get(ChannelDeliveryOutbox, uuid.UUID(result['_permission_feedback_notice_id']))
        if notice is None or notice.tenant_id != item.tenant_id or notice.agent_id != item.agent_id:
            return generic
        detail = ((notice.delivery_receipts_json or {}).get('text') or {}).get('detail') or {}
        target = notice.delivery_target_json or {}
        if (notice.channel != 'feishu' or notice.channel_config_id != config.id
            or detail.get('provider_message_id') != message.get('parent_id') or detail.get('provider_message_scope') != scope
            or target.get('receive_id_type') != 'open_id' or target.get('receive_id') != sender):
            return generic
        principal = await db.get(ExternalPrincipal, notice.external_principal_id) if notice.external_principal_id else None
        if principal is None or principal.subject_id != sender or principal.installation_ref != item.installation_ref:
            return generic
        actor = await load_external_runtime_actor(db, tenant_id=item.tenant_id, principal_id=principal.id,
            expected_user_id=notice.user_id)
        if not actor.authority_bound or actor.id != notice.user_id:
            return generic
        user = await load_scoped_runtime_user(db, actor.id, tenant_id=item.tenant_id)
        run = await db.get(RuntimeTask, notice.runtime_task_id)
        if (user is None or run is None or run.tenant_id != item.tenant_id or run.root_user_id != user.id
            or run.parent_agent_id != item.agent_id or run.parent_session_id != str(notice.session_id)):
            return generic
        work = await _original_work(db, run)
        if work is None:
            return generic
        session, record, _binding, _target = work
        await authorize_session_action(db, user, agent_id=item.agent_id, session_id=session.id,
            action='read_content_input_source', audit=False)
        if run.task_type == 'business_task':
            with source_recipient_read(session_id=session.id, user_id=user.id):
                await authorize_resource_action(db, user, agent_id=record.agent_id, resource_kind='task', resource_id=record.id,
                    action='read', owner_user_id=record.created_by, root_session_id=record.root_session_id,
                    authority_state=record.authority_state)
        anchor = f'session_id={session.id}#chat' if run.task_type == 'delegation' else f'task_id={record.id}#tasks'
        return (f'{base}/agents/{item.agent_id}?{anchor}' if base else '', str(notice.id))
    except (KeyError, ValueError, TypeError, HTTPException, ContentInputReceiptError):
        return generic


async def deliver_permission_reply_feedback(db, *, item, result):
    from app.database import pin_rls_tenant_context
    from app.services.channel_delivery_service import ChannelDeliveryService
    from app.services.channel_ingress_inbox import channel_installation_ref
    from app.services.channel_provider_receipts import feishu_message_scope

    unavailable = {'schema': SCHEMA, 'status': 'unavailable', 'retryable': False, 'recovery_action': 'send_fresh_reply'}
    await pin_rls_tenant_context(db, item.tenant_id)
    row = await db.scalar(select(ChannelIngressEvent).where(ChannelIngressEvent.id == item.id,
        ChannelIngressEvent.tenant_id == item.tenant_id, ChannelIngressEvent.agent_id == item.agent_id).with_for_update())
    if (row is None or row.status != 'processing' or row.attempt_count != item.attempt_count
        or row.provider != 'feishu' or row.handler_key != 'feishu.event'
        or row.payload_digest != item.payload_digest or row.installation_ref != item.installation_ref):
        return unavailable
    saved = dict((row.metadata_json or {}).get(KEY) or {})
    if saved.get('status') in {'delivered', 'unavailable', 'denied', 'needs_reconciliation'}:
        return _public_receipt(saved)
    if saved.get('status') == 'sending':
        saved.update(status='needs_reconciliation', retryable=False, recovery_action='check_channel_message')
        row.metadata_json = {**row.metadata_json, KEY: saved}
        await db.commit()
        return _public_receipt(saved)
    if saved.get('schema') != SCHEMA or saved.get('rejection') != {
        key: result[key] for key in ('permission_status', 'reason_code', '_permission_feedback_notice_id') if key in result}:
        return unavailable
    body = (row.payload_json or {}).get('body') or row.payload_json or {}
    event = body.get('event') or {}
    message = event.get('message') or {}
    sender = ((event.get('sender') or {}).get('sender_id') or {}).get('open_id')
    if not sender or message.get('chat_type') != 'p2p' or not message.get('parent_id'):
        return unavailable
    target = {'channel': 'feishu', 'receive_id_type': 'open_id', 'receive_id': sender}
    if (row.metadata_json or {}).get('tenant_channel_config_id'):
        from app.models.external_principal import ExternalPrincipal

        try:
            config_id = uuid.UUID(str(row.metadata_json['tenant_channel_config_id']))
        except (ValueError, TypeError):
            return unavailable
        principal_id = await db.scalar(select(ExternalPrincipal.id).where(
            ExternalPrincipal.tenant_id == item.tenant_id, ExternalPrincipal.provider == 'feishu',
            ExternalPrincipal.tenant_channel_config_id == config_id, ExternalPrincipal.channel_config_id.is_(None),
            ExternalPrincipal.installation_ref == item.installation_ref, ExternalPrincipal.subject_id == sender,
            ExternalPrincipal.status == 'active'))
        if principal_id is None:
            return unavailable
        target.update(tenant_channel_config_id=row.metadata_json['tenant_channel_config_id'], installation_ref=item.installation_ref)
        target['external_principal_id'] = str(principal_id)
    config = await ChannelDeliveryService._load_target_config(db, item.agent_id, 'feishu', target)
    scope = (row.metadata_json or {}).get('feishu_message_scope')
    if (config is None or not config.is_configured or config.tenant_id != item.tenant_id
        or channel_installation_ref(config, fallback='') != item.installation_ref
        or scope is None or scope != feishu_message_scope(config)):
        return unavailable
    url, notice_id = await _recovery_entry(db, item=item, result=result, config=config, sender=sender, message=message, scope=scope)
    text = feedback_text(result, url)
    receipt = {**saved, 'status': 'sending',
        'provider_message_scope': scope, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
        'idempotency_key': f'{item.id}:permission-reply-feedback'}
    # Only a proven current reader receives an exact work link. The audit stays
    # attached to the ingress event; this is never an input to the private work.
    receipt['original_work_link_available'] = notice_id is not None
    row.metadata_json = {**row.metadata_json, KEY: receipt}
    await db.commit()
    await pin_rls_tenant_context(db, item.tenant_id)
    await db.refresh(config, with_for_update=True)
    # Keep the actual application credentials stable through the existing send
    # boundary. A replacement must not receive an old app-scoped sender ID.
    if (not config.is_configured or feishu_message_scope(config) != scope
        or channel_installation_ref(config, fallback='') != item.installation_ref):
        receipt.update(status='denied', recovery_action='send_fresh_reply')
    else:
        delivery = await ChannelDeliveryService.send_text(db=db, agent_id=item.agent_id, tenant_id=item.tenant_id,
            reply_target=target, text=text, delivery_mode='permission_rejection_feedback',
            extra_detail={'channel_ingress_event_id': str(item.id)})
        provider_id = (delivery.detail or {}).get('provider_message_id')
        if delivery.ok and isinstance(provider_id, str) and provider_id.strip():
            receipt.update(status='delivered', provider_message_id=provider_id, recovery_action=None)
        elif delivery.status in {'unavailable', 'denied'}:
            receipt.update(status=delivery.status, recovery_action='send_fresh_reply')
        else:
            receipt.update(status='needs_reconciliation', recovery_action='check_channel_message')
    await pin_rls_tenant_context(db, item.tenant_id)
    row = await db.get(ChannelIngressEvent, item.id, populate_existing=True, with_for_update=True)
    if ((row.metadata_json or {}).get(KEY) or {}).get('audit_event_id') == receipt['audit_event_id']:
        row.metadata_json = {**row.metadata_json, KEY: receipt}
    await db.commit()
    return _public_receipt(receipt)
