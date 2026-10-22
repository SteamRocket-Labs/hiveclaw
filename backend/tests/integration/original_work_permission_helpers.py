"""Drive a provider quote through the durable authenticated ingress fixture."""

import json
import uuid
from contextlib import asynccontextmanager

from app.database import pin_rls_tenant_context, tenant_scoped_session
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
from app.services.channel_ingress_inbox import ChannelIngressInboxService, ChannelIngressSubmission, enqueue_channel_ingress_event


async def claim_permission_reply(*, factory, tenant_id, notice_id, text, sender=None, provider_reference=None):
    async with factory() as db:
        await pin_rls_tenant_context(db, tenant_id)
        notice = await db.get(ChannelDeliveryOutbox, notice_id)
        provider_id = provider_reference or notice.delivery_receipts_json['text']['detail'].get('provider_message_id')
        assert provider_id
        from app.services.channel_delivery_service import ChannelDeliveryService
        from app.services.channel_ingress_inbox import channel_installation_ref
        from app.services.channel_provider_receipts import feishu_message_scope

        config = await ChannelDeliveryService._load_target_config(db, notice.agent_id, notice.channel, notice.delivery_target_json)
        metadata = {'feishu_message_scope': feishu_message_scope(config)}
        if notice.delivery_target_json.get('tenant_channel_config_id'):
            metadata['tenant_channel_config_id'] = str(config.id)
        reply = {'header': {'event_type': 'im.message.receive_v1', 'event_id': uuid.uuid4().hex},
            'event': {'sender': {'sender_id': {'open_id': sender or notice.delivery_target_json['receive_id']}},
                'message': {'message_type': 'text', 'chat_type': 'p2p', 'message_id': uuid.uuid4().hex,
                    'parent_id': provider_id, 'content': json.dumps({'text': text})}}}
        accepted = await enqueue_channel_ingress_event(db, ChannelIngressSubmission(tenant_id=tenant_id,
            agent_id=notice.agent_id, provider='feishu', installation_ref=channel_installation_ref(config, fallback=''),
            provider_event_id=reply['header']['event_id'], handler_key='feishu.event', payload={'body': reply}, metadata=metadata))
        await db.commit()
    service = ChannelIngressInboxService(session_factory=factory)
    @asynccontextmanager
    async def scoped(_operation):
        async with tenant_scoped_session(tenant_id, session_factory=factory) as db:
            yield db
    service._worker_session = scoped
    items = await service.claim_batch(worker_id='permission-quote', limit=10)
    assert [item.id for item in items] == [accepted.event_id]
    return service, items[0]


async def dispatch_permission_rejection(*, factory, tenant_id, notice_id, item, exact_work, unknown_delivery=False, before_replay=None):
    from unittest.mock import patch

    from sqlalchemy import select
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.services.feishu_service import FeishuService

    models = [RuntimeTask, ChatSession, ChatMessage]
    async with factory() as db:
        await pin_rls_tenant_context(db, tenant_id)
        notice = await db.get(ChannelDeliveryOutbox, notice_id)
        before = {m.__name__: set(await db.scalars(select(m.id).where(m.tenant_id == tenant_id))) for m in models}
        session_owners = {(s.id, s.user_id, s.external_principal_id) for s in await db.scalars(
            select(ChatSession).where(ChatSession.tenant_id == tenant_id))}
        work_id = notice.metadata_json.get('business_task_id') or str(notice.session_id)
        target = notice.delivery_target_json
    sent = []
    async def transport(_self, _app_id, _secret, recipient, message_type, content, **kwargs):
        sent.append((recipient, message_type, json.loads(content)['text'], kwargs['receive_id_type']))
        return {} if unknown_delivery else {'data': {'message_id': 'synthetic-permission-rejection-feedback'}}
    with patch.object(FeishuService, 'send_message', transport):
        receipt = await dispatch_channel_ingress_event(item)
        assert receipt.get('permission_feedback', {}).get('status') == ('needs_reconciliation' if unknown_delivery else 'delivered'), receipt
        if before_replay is not None:
            before_replay()
        replay = await dispatch_channel_ingress_event(item)
        assert replay['permission_feedback'] == receipt['permission_feedback'], replay
    assert len(sent) == 1 and sent[0][:2] == (target['receive_id'], 'text') and sent[0][3] == 'open_id', sent
    text = sent[0][2]
    assert 'HiveClaw' in text
    assert not any(value in text for value in ('PRIVATE_BUSINESS_INPUT', 'DELEGATED_REPORT', 'UNATTESTED_REPLY_SCOPE'))
    if exact_work:
        assert work_id in text
    else:
        assert work_id not in text and str(notice.runtime_task_id) not in text and notice.prompt_key not in text
    assert receipt.get('runtime_task_id') is None and receipt.get('session_id') is None, receipt
    async with factory() as db:
        await pin_rls_tenant_context(db, tenant_id)
        after = {m.__name__: set(await db.scalars(select(m.id).where(m.tenant_id == tenant_id))) for m in models}
        assert after == before
        assert {(s.id, s.user_id, s.external_principal_id) for s in await db.scalars(
            select(ChatSession).where(ChatSession.tenant_id == tenant_id))} == session_owners
    return receipt


async def resolve_permission_reply(**kwargs):
    service, item = await claim_permission_reply(**kwargs)
    receipt = await dispatch_channel_ingress_event(item)
    assert receipt.get('permission_receipt'), receipt
    replay = await dispatch_channel_ingress_event(item)
    assert replay.get('permission_receipt') == receipt['permission_receipt'], replay
    assert await service._mark_processed(item=item, worker_id='permission-quote', receipt=receipt)
    repeated_service, repeated_item = await claim_permission_reply(**kwargs)
    repeated = await dispatch_channel_ingress_event(repeated_item)
    assert repeated.get('permission_receipt') == receipt['permission_receipt'], repeated
    assert await repeated_service._mark_processed(item=repeated_item, worker_id='permission-quote', receipt=repeated)
    await assert_permission_response_notice(factory=kwargs['factory'], tenant_id=kwargs['tenant_id'],
        notice_id=kwargs['notice_id'], receipt=receipt['permission_receipt'])
    return receipt


async def assert_permission_response_notice(*, factory, tenant_id, notice_id, receipt):
    from dataclasses import replace
    from datetime import UTC, datetime
    from unittest.mock import patch

    import pytest
    from sqlalchemy import select
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService, _claimed
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.feishu_service import FeishuService

    async with factory() as db:
        await pin_rls_tenant_context(db, tenant_id)
        original = await db.get(ChannelDeliveryOutbox, notice_id)
        notices = list(await db.scalars(select(ChannelDeliveryOutbox).where(
            ChannelDeliveryOutbox.runtime_task_id == original.runtime_task_id,
            ChannelDeliveryOutbox.metadata_json['source'].astext == 'original_work_permission_response')))
        assert len(notices) == 1, [(str(row.id), row.metadata_json) for row in notices]
        notice = notices[0]
        assert notice.session_id == original.session_id and notice.user_id == original.user_id
        assert notice.delivery_target_json == original.delivery_target_json
        assert notice.metadata_json['permission_receipt'] == receipt
        assert '此提示不代表任务完成' in notice.text_content
        assert str(original.agent_id) in notice.text_content
        if receipt['status'] == 'needs_reconciliation':
            assert '尚未确认' in notice.text_content and '不会自动重试' in notice.text_content
        assert not any(value in notice.text_content for value in ('PRIVATE_BUSINESS_INPUT', 'DELEGATED_REPORT', 'UNATTESTED'))
        saved = _claimed(notice)
        service = ChannelDeliveryOutboxService(session_factory=factory)
        for changed in (
            replace(saved, text=saved.text + '\nUNATTESTED'),
            replace(saved, delivery_target={**saved.delivery_target, 'receive_id': 'another-recipient'}),
            replace(saved, metadata={k: v for k, v in saved.metadata.items() if k != 'source'}),
            replace(saved, metadata={**saved.metadata, 'audit_event_id': str(uuid.uuid4())}),
        ):
            with pytest.raises(ContentInputReceiptError):
                await service._authorize_native_result_text(db, changed)
        from app.services.channel_delivery_service import ChannelDeliveryService

        config = await ChannelDeliveryService._load_target_config(db, original.agent_id, original.channel, original.delivery_target_json)
        original_app = config.app_id
        try:
            config.app_id = original_app + '-replaced'
            await db.flush()
            with pytest.raises(ContentInputReceiptError):
                await service._authorize_native_result_text(db, saved)
        finally:
            config.app_id = original_app
            await db.flush()
        # Claim this exact acknowledgement without consuming the independent
        # task-status notice queued by an uncertain native tool execution.
        notice.status, notice.locked_by, notice.locked_at = 'processing', 'permission-response-notice', datetime.now(UTC)
        notice.attempt_count += 1
        await db.commit()
        claimed = _claimed(notice)
    @asynccontextmanager
    async def scoped(_operation):
        async with tenant_scoped_session(tenant_id, session_factory=factory) as db:
            yield db
    service._worker_session = scoped
    sent = []
    async def transport(_self, _app_id, _secret, recipient, message_type, content, **kwargs):
        sent.append((recipient, message_type, json.loads(content)['text'], kwargs['receive_id_type']))
        return {'data': {'message_id': 'synthetic-permission-response-notice'}}
    with patch.object(FeishuService, 'send_message', transport):
        delivered = await service._deliver(claimed, worker_id='permission-response-notice')
        assert await service._mark_delivered(item_id=claimed.id, worker_id='permission-response-notice', receipt=delivered)
        async with factory() as db:
            await pin_rls_tenant_context(db, tenant_id)
            current = await db.get(ChannelDeliveryOutbox, claimed.id)
            assert current.status == 'delivered'
            assert current.delivery_receipts_json['text']['detail']['provider_message_id'] == 'synthetic-permission-response-notice'
            repeated = _claimed(current)
        await service._deliver(repeated, worker_id='permission-response-notice')
    assert sent == [(original.delivery_target_json['receive_id'], 'text', saved.text, 'open_id')]
