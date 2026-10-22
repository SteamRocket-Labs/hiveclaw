"""Durable card decisions reach only the original confirmed provider message."""

import hashlib
import json
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.audit import ApprovalRequest, AuditLog
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.notification import Notification
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from app.services.channel_ingress_dispatcher import ChannelIngressReplayRequest, dispatch_channel_ingress_event
from app.services.channel_ingress_inbox import ChannelIngressInboxService
from tests.integration.test_feishu_enterprise_cards import _enterprise_card_world, _request_card

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('mode', ['normal', 'reject', 'crash', 'retry', 'unknown', 'foreign_message',
    'replaced', 'revoked', 'missing_reference', 'decision_commit_crash', 'sending_crash', 'server_error'])
async def test_durable_decision_updates_its_confirmed_card_without_repeating_the_approval(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, mode,
):
    import app.services.feishu_service as service_module
    from app.api.feishu import feishu_card_callback
    from app.services.channel_ingress_context import use_channel_ingress_context
    from app.services.channel_provider_receipts import ProviderReceiptError

    tenant_id, user_id, agent_id, config, _provider_id, _sent, web = await _enterprise_card_world(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    message_id, patches, cards = 'om-' + uuid4().hex, [], []

    def provider(request):
        assert request.url.host == 'open.feishu.cn'
        if '/auth/v3/' in request.url.path:
            assert json.loads(request.content) == {'app_id': config.app_id, 'app_secret': config.app_secret}
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'synthetic-card-result-token'})
        assert request.headers['Authorization'] == 'Bearer synthetic-card-result-token'
        if request.method == 'POST':
            cards.append(json.loads(request.content))
            return httpx.Response(200, json={'code': 0, 'data': {} if mode == 'missing_reference' else {'message_id': message_id}})
        assert request.method == 'PATCH' and request.url.path == f'/open-apis/im/v1/messages/{message_id}'
        patches.append(json.loads(json.loads(request.content)['content']))
        if mode == 'retry' and len(patches) == 1:
            raise httpx.ConnectError('Synthetic pre-send connection failure', request=request)
        if mode == 'unknown':
            raise httpx.ReadTimeout('Synthetic ambiguous update response', request=request)
        if mode == 'sending_crash':
            raise asyncio.CancelledError('Synthetic stop during provider update')
        if mode == 'server_error':
            return httpx.Response(500, json={'code': 999999, 'msg': 'Synthetic ambiguous server failure'})
        return httpx.Response(200, json={'code': 0, 'msg': 'success', 'data': {}})

    factory = httpx.AsyncClient
    monkeypatch.setattr(service_module, 'httpx', SimpleNamespace(**{**vars(httpx), 'AsyncClient': lambda **kwargs:
        factory(**{**kwargs, 'transport': httpx.MockTransport(provider)})}))
    approval_id = await _request_card(app_user_sessionmaker, tenant_id, agent_id, user_id)
    assert len(cards) == 1
    card = json.loads(cards[0]['content'])
    callback = {'operator': {'open_id': 'ou-enterprise-card-owner', 'user_id': 'enterprise-card-owner'},
        'context': {'open_message_id': 'om-unrelated' if mode == 'foreign_message' else message_id},
        'action': {'value': card['elements'][-1]['actions'][int(mode == 'reject')]['value']}}
    body = {'header': {'event_type': 'card.action.trigger', 'event_id': uuid4().hex, 'app_id': config.app_id}, 'event': callback}
    raw = json.dumps(body, separators=(',', ':'))
    timestamp, nonce = '1792687000', 'synthetic-card-result'
    headers = {'Content-Type': 'application/json', 'X-Lark-Request-Timestamp': timestamp, 'X-Lark-Request-Nonce': nonce,
        'X-Lark-Signature': hashlib.sha256((timestamp + nonce + config.encrypt_key + raw).encode()).hexdigest()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url='http://card-result') as api:
        received = await api.post(f'/api/channel/feishu/tenant/{tenant_id}/webhook', content=raw, headers=headers)
        assert received.status_code == 200 and received.json()['routed'] is True
    service = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    items = await service.claim_batch(worker_id='card-result')
    assert len(items) == 1
    item = items[0]
    if mode in {'crash', 'replaced', 'revoked', 'decision_commit_crash'}:
        # Stop after the real decision commit, before result delivery or inbox ACK.
        with use_channel_ingress_context(event_id=item.id, tenant_id=tenant_id, agent_id=agent_id,
            provider='feishu', installation_ref=item.installation_ref):
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, tenant_id)
                if mode == 'decision_commit_crash':
                    commit = db.commit
                    async def interrupted_commit():
                        await commit()
                        raise RuntimeError('Synthetic stop immediately after the actual SQL commit')
                    monkeypatch.setattr(db, 'commit', interrupted_commit)
                answer = await feishu_card_callback(ChannelIngressReplayRequest(event_id=item.id, body=callback), db)
                if mode == 'decision_commit_crash':
                    assert answer['toast']['type'] == 'error'
                else:
                    assert answer['toast']['type'] == 'success'
        assert not patches
        if mode in {'replaced', 'revoked'}:
            async with owner_sessionmaker() as db:
                if mode == 'replaced':
                    (await db.get(TenantChannelConfig, config.id)).app_id = 'synthetic-new-application'
                else:
                    (await db.get(User, user_id)).is_active = False
                await db.commit()
    if mode == 'retry':
        with pytest.raises(ProviderReceiptError) as refused:
            await dispatch_channel_ingress_event(item)
        assert refused.value.retryable and refused.value.status == 'unavailable'
        assert await service._mark_failed(item=item, worker_id='card-result', error=refused.value) == 'retry'
        async with owner_sessionmaker() as db:
            (await db.get(ChannelIngressEvent, item.id)).available_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        item, = await service.claim_batch(worker_id='card-result')
        assert item.attempt_count == 2
    if mode == 'sending_crash':
        with pytest.raises(asyncio.CancelledError):
            await dispatch_channel_ingress_event(item)
    result = await dispatch_channel_ingress_event(item)
    if mode == 'replaced':
        assert result['status'] == 'denied' and not patches
    else:
        delivery = result['card_delivery']
        expected = ('needs_reconciliation' if mode in {'unknown', 'sending_crash', 'server_error'} else 'denied' if mode == 'revoked'
            else 'unavailable' if mode in {'foreign_message', 'missing_reference'} else 'delivered')
        assert delivery['status'] == expected
        assert len(patches) == (0 if mode in {'revoked', 'foreign_message', 'missing_reference'} else 2 if mode == 'retry' else 1)
        again = await dispatch_channel_ingress_event(item)
        assert again['card_delivery'] == delivery
        assert len(patches) == (0 if mode in {'revoked', 'foreign_message', 'missing_reference'} else 2 if mode == 'retry' else 1)
        if expected == 'delivered':
            assert delivery['provider_reference'] == message_id
            assert ('已拒绝' if mode == 'reject' else '已批准') in patches[-1]['elements'][0]['text']['content']
        else:
            assert delivery['recovery_action'] == 'continue_in_original_web_work'
    assert await service._mark_processed(item=item, worker_id='card-result', receipt=result)
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        assert approval.status == ('rejected' if mode == 'reject' else 'approved') and approval.resolved_by == user_id
        audits = list(await db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == 'approval_approved')))
        assert len(audits) == int(mode != 'reject')
        notices = list(await db.scalars(select(Notification).where(Notification.ref_id == approval_id,
            Notification.type == 'approval_resolved')))
        if mode == 'decision_commit_crash':
            pending = await db.scalar(select(Notification).where(Notification.ref_id == approval_id,
                Notification.type == 'approval_pending'))
            assert pending is not None and pending.link == f'/agents/{agent_id}#approvals'
            assert not notices
        else:
            assert len(notices) == 1 and notices[0].link == f'/agents/{agent_id}#approvals'
        assert (await db.get(ChannelIngressEvent, item.id)).processing_receipt_json == result
    assert (await service.drain_once(worker_id='card-result-no-replay'))['claimed'] == 0
