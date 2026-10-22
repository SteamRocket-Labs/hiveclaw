"""SDK callbacks retain the installation that authenticated their connection."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.tenant import Tenant
from app.models.user import User

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('kind', ['message', 'card'])
@pytest.mark.parametrize('change', ['application', 'credentials', 'platform', 'paused', 'replaced', 'stopped'])
async def test_captured_sdk_callback_cannot_borrow_a_new_installation(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, kind, change,
):
    from app.services import feishu_ws
    from app.services.channel_provider_receipts import feishu_message_scope

    tenant_id, user_id, agent_id, config_id = (uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name='SDK origin', slug=uuid4().hex))
        db.add(User(id=user_id, tenant_id=tenant_id, username=uuid4().hex,
            email=f'{uuid4().hex}@synthetic.invalid', password_hash='synthetic', display_name='SDK owner'))
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name='SDK employee', role_description='Serve authenticated work',
            creator_id=user_id, owner_user_id=user_id, sponsor_user_id=user_id))
        await db.flush()
        config = ChannelConfig(id=config_id, tenant_id=tenant_id, agent_id=agent_id, channel_type='feishu',
            app_id='synthetic-sdk-app', app_secret='synthetic-sdk-secret', is_configured=True,
            extra_config={'connection_mode': 'websocket'})
        db.add(config)
        await db.commit()

    clients = []
    class Builder:
        def __init__(self):
            self.handlers = {}
        def register_p2_customized_event(self, name, handler):
            self.handlers[name] = handler
            return self
        def build(self):
            return self.handlers
    class Client:
        def __init__(self, app_id, app_secret, *, event_handler, **kwargs):
            self.handlers, self.connected, self._conn = event_handler, asyncio.Event(), None
            clients.append(self)
        async def _connect(self):
            self._conn = SimpleNamespace(closed=False)
            self.connected.set()
        async def _disconnect(self):
            self._conn = None
        async def _ping_loop(self):
            return None

    monkeypatch.setattr(feishu_ws, '_HAS_LARK', True)
    monkeypatch.setattr(feishu_ws, '_PROXY_PATCH_AVAILABLE', False)
    monkeypatch.setattr(feishu_ws, 'lark', SimpleNamespace(LogLevel=SimpleNamespace(INFO='info'),
        EventDispatcherHandler=SimpleNamespace(builder=lambda *_: Builder())))
    monkeypatch.setattr(feishu_ws, 'ws', SimpleNamespace(Client=Client))
    monkeypatch.setattr(feishu_ws, 'async_session', app_user_sessionmaker)
    monkeypatch.setattr('app.database.async_session', app_user_sessionmaker)
    monkeypatch.setattr('app.services.tenant_resolver.async_session', app_user_sessionmaker)
    manager = feishu_ws.FeishuWSManager()
    method = '_async_handle_message' if kind == 'message' else '_async_handle_card_action'
    handler_key = 'im.message.receive_v1' if kind == 'message' else 'card.action.trigger'
    original = getattr(manager, method)
    finished = asyncio.Event()
    async def observed(*args, **kwargs):
        try:
            await original(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(manager, method, observed)

    async def fire(handler, event_id, *, app_id='synthetic-sdk-app'):
        finished.clear()
        handler({'header': {'event_id': event_id, 'event_type': handler_key, 'app_id': app_id},
            'event': {'operator': {'open_id': 'ou_sdk'}, 'sender': {'sender_id': {'open_id': 'ou_sdk'}},
                'message': {'message_type': 'text', 'message_id': event_id, 'content': '{"text":"SDK work"}'}}})
        await asyncio.wait_for(finished.wait(), 5)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            return await db.scalar(select(ChannelIngressEvent).where(ChannelIngressEvent.tenant_id == tenant_id,
                ChannelIngressEvent.provider_event_id == event_id))

    try:
        await manager.start_all()
        old_client = clients[-1]
        await asyncio.wait_for(old_client.connected.wait(), 5)
        old_connection = manager._connections[agent_id]
        handler = old_client.handlers[handler_key]
        first = await fire(handler, 'sdk-before-change')
        assert first is not None and first.installation_ref == str(config_id)
        assert first.handler_key == ('feishu.event' if kind == 'message' else 'feishu.card_action')
        assert first.metadata_json['feishu_message_scope'] == feishu_message_scope(config)

        if change in {'stopped', 'replaced'}:
            task = manager._tasks[agent_id]
            await manager.stop_client(agent_id)
            await task
        async with owner_sessionmaker() as db:
            current = await db.get(ChannelConfig, config_id)
            if change == 'application':
                current.app_id += '-new'
            elif change == 'credentials':
                current.app_secret += '-new'
            elif change == 'platform':
                current.extra_config = {**current.extra_config, 'platform_region': 'lark_global'}
            elif change == 'paused':
                current.is_configured = False
            elif change == 'replaced':
                await db.delete(current)
                await db.flush()
                db.add(ChannelConfig(id=uuid4(), tenant_id=tenant_id, agent_id=agent_id, channel_type='feishu',
                    app_id=config.app_id, app_secret=config.app_secret, is_configured=True,
                    extra_config={'connection_mode': 'websocket'}))
            await db.commit()
        assert await fire(handler, 'sdk-after-change') is None
        async with owner_sessionmaker() as db:
            current = await db.scalar(select(ChannelConfig).where(ChannelConfig.agent_id == agent_id))
            current.is_configured = True
            await db.commit()
        await manager.start_all()
        new_client = clients[-1]
        assert new_client is not old_client
        await asyncio.wait_for(new_client.connected.wait(), 5)
        await manager._mark_channel_status(agent_id, is_connected=True, connection_status='connected',
            connection=manager._connections[agent_id])
        await manager._mark_channel_status(agent_id, is_connected=False, is_configured=False,
            connection_status='invalid_credentials', connection=old_connection)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            observed_config = await db.get(ChannelConfig, current.id)
            assert observed_config.is_configured and observed_config.is_connected
            assert observed_config.extra_config['connection_status'] == 'connected'
        assert await fire(handler, 'sdk-old-after-restart') is None
        fresh = await fire(new_client.handlers[handler_key], 'sdk-new-after-restart', app_id=current.app_id)
        assert fresh is not None and fresh.installation_ref == str(current.id)
        assert fresh.handler_key == first.handler_key
        assert fresh.metadata_json['feishu_message_scope'] == feishu_message_scope(current)
        assert await fire(new_client.handlers[handler_key], 'sdk-foreign-header', app_id='synthetic-other-app') is None
        if change in {'application', 'credentials', 'platform'}:
            current_task = manager._tasks[agent_id]
            count = len(clients)
            await manager.start_client(agent_id, config.app_id, config.app_secret, extra_config=config.extra_config)
            assert len(clients) == count and manager._tasks[agent_id] is current_task
    finally:
        tasks = list(manager._tasks.values())
        await manager.stop_client(agent_id)
        await asyncio.gather(*tasks, return_exceptions=True)
        async with owner_sessionmaker() as db:
            (await db.get(Tenant, tenant_id)).is_active = False
            current = await db.scalar(select(ChannelConfig).where(ChannelConfig.agent_id == agent_id))
            if current is not None:
                current.is_configured = False
            await db.commit()
