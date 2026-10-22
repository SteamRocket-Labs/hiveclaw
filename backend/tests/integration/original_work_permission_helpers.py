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
        reply = {'header': {'event_type': 'im.message.receive_v1', 'event_id': uuid.uuid4().hex},
            'event': {'sender': {'sender_id': {'open_id': sender or notice.delivery_target_json['receive_id']}},
                'message': {'message_type': 'text', 'chat_type': 'p2p', 'message_id': uuid.uuid4().hex,
                    'parent_id': provider_id, 'content': json.dumps({'text': text})}}}
        accepted = await enqueue_channel_ingress_event(db, ChannelIngressSubmission(tenant_id=tenant_id,
            agent_id=notice.agent_id, provider='feishu', installation_ref=str(notice.channel_config_id),
            provider_event_id=reply['header']['event_id'], handler_key='feishu.event', payload={'body': reply}))
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


async def resolve_permission_reply(**kwargs):
    service, item = await claim_permission_reply(**kwargs)
    receipt = await dispatch_channel_ingress_event(item)
    assert receipt.get('permission_receipt'), receipt
    replay = await dispatch_channel_ingress_event(item)
    assert replay.get('permission_receipt') == receipt['permission_receipt'], replay
    assert await service._mark_processed(item=item, worker_id='permission-quote', receipt=receipt)
    return receipt
