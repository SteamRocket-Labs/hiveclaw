"""One enterprise installation can deliver and resolve its scoped approvals."""

import hashlib
import json
from uuid import UUID, uuid4
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.tenant_middleware import TenantMiddleware
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog
from app.models.channel_config import ChannelConfig
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.external_principal import ExternalPrincipal
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.notification import Notification
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from app.services.approval_service import approval_service
from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
from app.services.channel_ingress_inbox import ChannelIngressInboxService, channel_installation_ref
from tests.services.test_channel_ingress_inbox import _seed_agent

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


async def _enterprise_card_world(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.api.tenant_channels import router
    import app.services.feishu_service as service_module

    tenant_id, user_id, agent_id = await _seed_agent(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        config = TenantChannelConfig(id=uuid4(), tenant_id=tenant_id, channel_type='feishu', is_active=True,
            app_id='synthetic-enterprise-card-app', app_secret='synthetic-enterprise-card-secret',
            encrypt_key='synthetic-enterprise-card-key')
        provider = IdentityProvider(id=uuid4(), tenant_id=tenant_id, provider_type='feishu', name='Card directory',
            is_active=True, config={'app_id': config.app_id})
        db.add_all([config, provider])
        await db.flush()
        db.add(ExternalIdentity(id=uuid4(), provider_id=provider.id, user_id=user_id,
            provider_open_id='ou-enterprise-card-owner', provider_user_id='enterprise-card-owner'))
        await db.commit()
    sent = []

    credentials = []

    def provider_transport(request):
        assert request.url.host == 'open.feishu.cn'
        if '/auth/v3/' in request.url.path:
            credentials.append(json.loads(request.content))
            assert credentials[-1] == {'app_id': config.app_id, 'app_secret': config.app_secret}
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'synthetic-enterprise-card-token'})
        assert request.url.path == '/open-apis/im/v1/messages' and request.method == 'POST'
        assert request.headers['Authorization'] == 'Bearer synthetic-enterprise-card-token'
        body = json.loads(request.content)
        sent.append({'app_id': credentials[-1]['app_id'], 'secret': credentials[-1]['app_secret'],
            'receive_id': body['receive_id'], 'msg_type': body['msg_type'], 'content': json.loads(body['content']),
            'receive_id_type': request.url.params['receive_id_type']})
        return httpx.Response(200, json={'code': 0, 'data': {'message_id': 'synthetic-enterprise-approval-card'}})

    client_factory = httpx.AsyncClient
    service_httpx = {**vars(httpx), 'AsyncClient': lambda **kwargs: client_factory(
        **{**kwargs, 'transport': httpx.MockTransport(provider_transport)})}
    monkeypatch.setattr(service_module, 'httpx', SimpleNamespace(**service_httpx))
    monkeypatch.setattr('app.database.async_session', app_user_sessionmaker)
    web = FastAPI()
    web.add_middleware(TenantMiddleware)
    web.include_router(router, prefix='/api')

    async def database():
        async with app_user_sessionmaker() as db:
            yield db
            await db.commit()

    web.dependency_overrides[get_db] = database
    return tenant_id, user_id, agent_id, config, provider.id, sent, web


async def _request_card(app_user_sessionmaker, tenant_id, agent_id, user_id):
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        result = await approval_service.request_approval(db, await db.get(Agent, agent_id), action_type='publish',
            details={'purpose': 'Enterprise approval in original employee work', 'requested_by': str(user_id)})
        await db.commit()
        return UUID(result['approval_id'])


@pytest.mark.parametrize('directory_matches', [True, False])
async def test_enterprise_only_request_delivers_the_real_card_to_its_current_directory(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, directory_matches,
):
    tenant_id, user_id, agent_id, config, provider_id, sent, _web = await _enterprise_card_world(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    if not directory_matches:
        async with owner_sessionmaker() as db:
            (await db.get(IdentityProvider, provider_id)).config = {'app_id': 'different-directory-app'}
            user = await db.get(User, user_id)
            user.feishu_open_id = 'ou-enterprise-card-owner'
            await db.commit()
    approval_id = await _request_card(app_user_sessionmaker, tenant_id, agent_id, user_id)
    async with owner_sessionmaker() as db:
        assert not list(await db.scalars(select(ChannelConfig).where(ChannelConfig.agent_id == agent_id)))
        notification = await db.scalar(select(Notification).where(Notification.ref_id == approval_id,
            Notification.user_id == user_id, Notification.type == 'approval_pending'))
        assert notification is not None and notification.link == f'/agents/{agent_id}#approvals'
    assert len(sent) == int(directory_matches)
    if directory_matches:
        card = sent[0]
        assert (card['app_id'], card['secret'], card['receive_id'], card['receive_id_type'], card['msg_type']) == (
            config.app_id, config.app_secret, 'ou-enterprise-card-owner', 'open_id', 'interactive')
        actions = card['content']['elements'][-1]['actions']
        assert [json.loads(action['value']) for action in actions] == [
            {'action': 'approve', 'approval_id': str(approval_id)}, {'action': 'reject', 'approval_id': str(approval_id)}]


@pytest.mark.parametrize('mode', ['signed', 'encrypted', 'reject', 'owner_changed', 'installation_replaced', 'cross_company'])
async def test_enterprise_card_webhook_persists_the_decision_in_the_original_employee_work(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, mode,
):
    tenant_id, user_id, agent_id, config, _provider_id, sent, web = await _enterprise_card_world(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    approval_id = await _request_card(app_user_sessionmaker, tenant_id, agent_id, user_id)
    target_id = approval_id
    if mode == 'cross_company':
        foreign_tenant, foreign_user, foreign_agent = await _seed_agent(owner_sessionmaker)
        async with owner_sessionmaker() as db:
            foreign = ApprovalRequest(id=uuid4(), tenant_id=foreign_tenant, agent_id=foreign_agent,
                action_type='publish', requested_by=foreign_user, status='pending', details={})
            db.add(foreign)
            await db.commit()
            target_id = foreign.id
    action = 'reject' if mode == 'reject' else 'approve'
    value = json.dumps({'action': action, 'approval_id': str(target_id)})
    if mode in {'signed', 'encrypted', 'reject'} and sent:
        value = sent[0]['content']['elements'][-1]['actions'][int(mode == 'reject')]['value']
    event_id = uuid4().hex
    event = {'header': {'event_type': 'card.action.trigger', 'event_id': event_id, 'app_id': config.app_id},
        'event': {'operator': {'open_id': 'ou-enterprise-card-owner', 'user_id': 'enterprise-card-owner'},
            'action': {'value': value}}}
    if mode == 'encrypted':
        from tests.api.test_feishu_webhook_security import _encrypt_feishu_payload
        event = {'encrypt': _encrypt_feishu_payload(config.encrypt_key, event)}
    raw = json.dumps(event, separators=(',', ':'))
    timestamp, nonce = '1792686000', 'synthetic-enterprise-card'
    headers = {'Content-Type': 'application/json', 'X-Lark-Request-Timestamp': timestamp,
        'X-Lark-Request-Nonce': nonce,
        'X-Lark-Signature': hashlib.sha256((timestamp + nonce + config.encrypt_key + raw).encode()).hexdigest()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url='http://enterprise-card') as client:
        response = await client.post(f'/api/channel/feishu/tenant/{tenant_id}/webhook', content=raw, headers=headers)
        assert response.status_code == 200, response.text
        if mode == 'cross_company':
            assert response.json().get('routed') is False, response.text
        else:
            assert response.json().get('routed') is True, response.text
            replay = await client.post(f'/api/channel/feishu/tenant/{tenant_id}/webhook', content=raw, headers=headers)
            assert replay.status_code == 200
    if mode == 'owner_changed':
        async with owner_sessionmaker() as db:
            other = User(id=uuid4(), tenant_id=tenant_id, username=uuid4().hex, email=f'{uuid4().hex}@synthetic.invalid',
                password_hash='synthetic', display_name='New owner')
            db.add(other)
            await db.flush()
            (await db.get(Agent, agent_id)).owner_user_id = other.id
            await db.commit()
    elif mode == 'installation_replaced':
        async with owner_sessionmaker() as db:
            (await db.get(TenantChannelConfig, config.id)).app_id = 'synthetic-replacement-app'
            await db.commit()
    service = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    items = await service.claim_batch(worker_id='enterprise-card-worker')
    if mode == 'cross_company':
        assert not items
    else:
        assert len(items) == 1 and items[0].agent_id == agent_id
        assert items[0].installation_ref == channel_installation_ref(config, fallback='')
        result = await dispatch_channel_ingress_event(items[0])
        assert await service._mark_processed(item=items[0], worker_id='enterprise-card-worker', receipt=result)
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, target_id)
        success = mode in {'signed', 'encrypted', 'reject'}
        assert approval.status == ('rejected' if mode == 'reject' else 'approved' if success else 'pending')
        assert approval.resolved_by == (user_id if success else None)
        audits = list(await db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id,
            AuditLog.action == 'approval_approved')))
        assert len(audits) == int(mode in {'signed', 'encrypted'})
        notifications = list(await db.scalars(select(Notification).where(Notification.ref_id == approval_id,
            Notification.user_id == user_id, Notification.type == 'approval_resolved')))
        assert len(notifications) == int(success)
        if success:
            assert len(sent) == 1 and notifications[0].link == f'/agents/{agent_id}#approvals'
            principal = await db.scalar(select(ExternalPrincipal).where(ExternalPrincipal.tenant_id == tenant_id,
                ExternalPrincipal.subject_id == 'ou-enterprise-card-owner'))
            assert principal.linked_user_id == user_id and principal.tenant_channel_config_id == config.id
            assert principal.channel_config_id is None
        if mode != 'cross_company':
            row = await db.get(ChannelIngressEvent, items[0].id)
            assert row.status == 'processed'
        else:
            assert (await db.get(ApprovalRequest, approval_id)).status == 'pending'
    assert (await service.drain_once(worker_id='enterprise-card-replay'))['claimed'] == 0
