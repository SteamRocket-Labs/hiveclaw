"""Queued provider input retains its authenticated application namespace."""

from uuid import uuid4

import pytest

from app.database import pin_rls_tenant_context
from app.models.channel_config import ChannelConfig
from app.models.channel_ingress_event import ChannelIngressEvent
from app.services.channel_ingress_inbox import ChannelIngressInboxService, accept_authenticated_channel_event
from app.services.channel_provider_receipts import feishu_message_scope
from tests.services.test_channel_ingress_inbox import _seed_agent

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('kind', ['message', 'card', 'recovery'])
@pytest.mark.parametrize('change', ['application', 'platform', 'paused', 'replaced', 'credentials', 'same', 'unstamped', 'corrupt'])
async def test_queued_feishu_input_rechecks_its_current_installation(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, kind, change,
):
    from app.api import feishu
    from app.services import channel_ingress_dispatcher as dispatcher

    tenant_id, _user_id, agent_id = await _seed_agent(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        config = ChannelConfig(id=uuid4(), tenant_id=tenant_id, agent_id=agent_id, channel_type='feishu',
            app_id='synthetic-queued-app', app_secret='synthetic-queued-secret', is_configured=True)
        db.add(config)
        await db.commit()
    monkeypatch.setattr('app.database.async_session', app_user_sessionmaker)
    calls = []
    async def consume(*args, **kwargs):
        calls.append(kind)
        return {'status': 'accepted'}
    async def resume(db, item):
        if kind == 'recovery':
            return await consume()
        return None
    monkeypatch.setattr(feishu, 'process_feishu_event', consume)
    monkeypatch.setattr(feishu, 'feishu_card_callback', consume)
    monkeypatch.setattr(dispatcher, '_resume_materialized_user_message', resume)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        receipt = await accept_authenticated_channel_event(db, tenant_id=tenant_id, agent_id=agent_id,
            provider='feishu', installation_ref=str(config.id), provider_event_id=uuid4().hex,
            handler_key='feishu.card_action' if kind == 'card' else 'feishu.event',
            body={'header': {'event_type': 'card.action.trigger' if kind == 'card' else 'im.message.receive_v1'},
                'event': {'message': {'message_type': 'text'}}},
            metadata={} if change == 'unstamped' else {
                'feishu_message_scope': 7 if change == 'corrupt' else feishu_message_scope(config)})
    async with owner_sessionmaker() as db:
        current = await db.get(ChannelConfig, config.id)
        if change == 'application':
            current.app_id += '-new'
        elif change == 'platform':
            current.extra_config = {'platform_region': 'lark_global'}
        elif change == 'credentials':
            current.app_secret += '-new'
        elif change == 'replaced':
            await db.delete(current)
            await db.flush()
            db.add(ChannelConfig(id=uuid4(), tenant_id=tenant_id, agent_id=agent_id, channel_type='feishu',
                app_id=config.app_id, app_secret=config.app_secret, is_configured=True))
        await db.commit()
    service = ChannelIngressInboxService(session_factory=app_user_sessionmaker, retry_base_seconds=0)
    async def dispatch(item):
        if change == 'paused':
            async with owner_sessionmaker() as db:
                (await db.get(ChannelConfig, config.id)).is_configured = False
                await db.commit()
        return await dispatcher.dispatch_channel_ingress_event(item)
    stats = await service.drain_once(worker_id='queued-current-installation', dispatch=dispatch)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        row = await db.get(ChannelIngressEvent, receipt.event_id)
        if change in {'same', 'credentials'}:
            assert stats['processed'] == 1 and row.status == 'processed' and calls == [kind]
            assert row.processing_receipt_json['status'] == ('accepted' if kind == 'recovery' else 'processed')
        elif change == 'paused':
            assert calls == []
            assert stats['deferred'] == 1 and row.status == 'received' and row.attempt_count == 0
            assert row.processing_receipt_json == {'status': 'unavailable', 'reason_code': 'feishu_installation_paused',
                'retryable': True, 'recovery_action': 'resume_channel_connection'}
        else:
            expected = 'unavailable' if change in {'unstamped', 'corrupt'} else 'denied'
            assert calls == []
            assert stats['processed'] == 1 and row.status == 'processed'
            assert row.processing_receipt_json['status'] == expected
            assert row.processing_receipt_json['retryable'] is False
            assert row.processing_receipt_json['recovery_action'] == 'send_new_channel_message'
    if change == 'paused':
        assert (await service.drain_once(worker_id='queued-still-paused'))['claimed'] == 0
        async with owner_sessionmaker() as db:
            (await db.get(ChannelConfig, config.id)).is_configured = True
            await db.commit()
        assert (await service.drain_once(worker_id='queued-current-installation'))['processed'] == 1
        assert calls == [kind]
    assert (await service.drain_once(worker_id='queued-current-installation'))['claimed'] == 0


@pytest.mark.parametrize('change', ['same', 'credentials', 'application', 'paused'])
async def test_versioned_enterprise_ingress_preserves_its_legacy_scope(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, change,
):
    from app.api import feishu
    from app.models.tenant_channel_config import TenantChannelConfig
    from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
    from app.services.channel_ingress_inbox import channel_installation_ref

    tenant_id, _user_id, agent_id = await _seed_agent(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        config = TenantChannelConfig(id=uuid4(), tenant_id=tenant_id, channel_type='feishu',
            app_id='synthetic-versioned-app', app_secret='synthetic-versioned-secret', is_active=True)
        db.add(config)
        await db.commit()
    monkeypatch.setattr('app.database.async_session', app_user_sessionmaker)
    calls = []
    async def consume(*args, **kwargs):
        calls.append(kwargs['tenant_channel_config'].id)
        return {'status': 'accepted'}
    monkeypatch.setattr(feishu, 'process_feishu_event', consume)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        receipt = await accept_authenticated_channel_event(db, tenant_id=tenant_id, agent_id=agent_id,
            provider='feishu', installation_ref=channel_installation_ref(config, fallback=''),
            provider_event_id=uuid4().hex, handler_key='feishu.event', body={'event': {}},
            metadata={'tenant_channel_config_id': str(config.id)})
    async with owner_sessionmaker() as db:
        current = await db.get(TenantChannelConfig, config.id)
        if change == 'application':
            current.app_id += '-replaced'
        elif change == 'credentials':
            current.app_secret += '-rotated'
        await db.commit()
    async def dispatch(item):
        if change == 'paused':
            async with owner_sessionmaker() as db:
                (await db.get(TenantChannelConfig, config.id)).is_active = False
                await db.commit()
        return await dispatch_channel_ingress_event(item)
    service = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    stats = await service.drain_once(worker_id='versioned-enterprise', dispatch=dispatch)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        event = await db.get(ChannelIngressEvent, receipt.event_id)
        if change == 'paused':
            assert stats['deferred'] == 1 and event.status == 'received' and event.attempt_count == 0
            assert event.processing_receipt_json['retryable'] is True and calls == []
        elif change == 'application':
            assert stats['processed'] == 1 and event.processing_receipt_json['status'] == 'denied' and calls == []
        else:
            assert stats['processed'] == 1 and event.status == 'processed' and calls == [config.id]
    if change == 'paused':
        assert (await service.drain_once(worker_id='enterprise-still-paused'))['claimed'] == 0
        async with owner_sessionmaker() as db:
            (await db.get(TenantChannelConfig, config.id)).is_active = True
            await db.commit()
        assert (await service.drain_once(worker_id='enterprise-resumed'))['processed'] == 1
        assert calls == [config.id]
    assert (await service.drain_once(worker_id='enterprise-duplicate'))['claimed'] == 0
