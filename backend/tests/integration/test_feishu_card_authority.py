"""Approval decisions require a proven callback identity in the target namespace."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.tenant_middleware import TenantMiddleware
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.services.external_principal_service import bind_authenticated_self_channel_principal
from tests.services.test_channel_ingress_inbox import _seed_agent

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


async def _card_world(owner_sessionmaker):
    tenant_id, user_id, agent_id = await _seed_agent(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        config = ChannelConfig(id=uuid4(), tenant_id=tenant_id, agent_id=agent_id, channel_type='feishu',
            app_id='synthetic-card-app', app_secret='synthetic-card-secret', encrypt_key='synthetic-card-key',
            verification_token='synthetic-card-token', is_configured=True)
        db.add(config)
        await db.flush()
        await bind_authenticated_self_channel_principal(db, tenant_id=tenant_id, config=config,
            provider_subject_id='ou-card-owner', user_id=user_id, actor_user_id=user_id)
        approval = ApprovalRequest(id=uuid4(), tenant_id=tenant_id, agent_id=agent_id, action_type='publish',
            requested_by=user_id, status='pending', execution_status='pending', details={'purpose': 'Native callback proof'})
        db.add(approval)
        await db.commit()
    return tenant_id, user_id, agent_id, config, approval.id


def _card_body(approval_id, action='approve'):
    return {'operator': {'open_id': 'ou-card-owner'},
        'action': {'value': json.dumps({'approval_id': str(approval_id), 'action': action})}}


@pytest.mark.parametrize('mode', ['unsigned', 'invalid_signature', 'signed', 'paused', 'raw_marker', 'invalid_action',
    'expired', 'token', 'invalid_token', 'inactive_company', 'reject'])
async def test_public_card_requires_provider_proof_before_the_real_decision(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, mode,
):
    from app.api.feishu import router

    tenant_id, user_id, _agent_id, config, approval_id = await _card_world(owner_sessionmaker)
    async def no_notify(*args, **kwargs):
        return None
    monkeypatch.setattr('app.services.notification_service.send_notification', no_notify)
    async def database():
        async with app_user_sessionmaker() as db:
            yield db
    app = FastAPI()
    app.add_middleware(TenantMiddleware)
    app.include_router(router, prefix='/api')
    app.dependency_overrides[get_db] = database
    if mode in {'paused', 'expired', 'token', 'invalid_token', 'inactive_company'}:
        async with owner_sessionmaker() as db:
            if mode == 'paused':
                (await db.get(ChannelConfig, config.id)).is_configured = False
            elif mode == 'expired':
                (await db.get(ApprovalRequest, approval_id)).created_at = datetime.now(UTC) - timedelta(days=8)
            elif mode == 'inactive_company':
                from app.models.tenant import Tenant
                (await db.get(Tenant, tenant_id)).is_active = False
            else:
                (await db.get(ChannelConfig, config.id)).encrypt_key = None
            await db.commit()
    body = _card_body(approval_id, 'unrecognized-action' if mode == 'invalid_action' else 'reject' if mode == 'reject' else 'approve')
    if mode in {'token', 'invalid_token'}:
        body['token'] = config.verification_token if mode == 'token' else 'wrong-token'
    raw = json.dumps(body, separators=(',', ':'))
    headers = {'Content-Type': 'application/json'}
    if mode not in {'unsigned', 'raw_marker', 'token', 'invalid_token'}:
        headers.update({'X-Lark-Request-Timestamp': '1792684000', 'X-Lark-Request-Nonce': 'synthetic-card-nonce'})
        signed = headers['X-Lark-Request-Timestamp'] + headers['X-Lark-Request-Nonce'] + config.encrypt_key + raw
        headers['X-Lark-Signature'] = 'invalid' if mode == 'invalid_signature' else hashlib.sha256(signed.encode()).hexdigest()
    if mode == 'raw_marker':
        headers['X-Channel-Ingress-Event-Id'] = str(uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://card') as client:
        response = await client.post('/api/channel/feishu/card-callback', content=raw, headers=headers)
    async with owner_sessionmaker() as db:
        saved = await db.get(ApprovalRequest, approval_id)
        audit = list(await db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id,
            AuditLog.action == 'approval_approved')))
        if mode in {'signed', 'token'}:
            assert saved.status == 'approved' and saved.resolved_by == user_id and len(audit) == 1
            assert response.status_code == 200 and response.json()['toast']['type'] == 'success', response.text
        elif mode == 'expired':
            assert saved.status == 'rejected' and not audit
            assert '已拒绝' in response.json()['card']['elements'][0]['text']['content'], response.text
            assert response.json()['toast']['type'] == 'error'
        elif mode == 'reject':
            assert saved.status == 'rejected' and saved.resolved_by == user_id and not audit
            assert response.json()['toast']['type'] == 'success'
        else:
            assert saved.status == 'pending' and saved.resolved_by is None and not audit, response.text
            assert response.status_code == 403 or response.json()['toast']['type'] == 'error', response.text


@pytest.mark.parametrize('foreign_company', [False, True])
async def test_card_replay_cannot_claim_another_installations_bound_user(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, foreign_company,
):
    from app.models.tenant import Tenant
    from app.services.channel_ingress_inbox import ChannelIngressInboxService, accept_authenticated_channel_event
    from app.services.channel_provider_receipts import feishu_message_scope
    from app.database import pin_rls_tenant_context

    tenant_id, user_id, agent_id, config, _approval_id = await _card_world(owner_sessionmaker)
    target_tenant = uuid4() if foreign_company else tenant_id
    target_user, target_agent = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        if foreign_company:
            db.add(Tenant(id=target_tenant, name='Other card company', slug=uuid4().hex))
        db.add(User(id=target_user, tenant_id=target_tenant, username=uuid4().hex,
            email=f'{uuid4().hex}@synthetic.invalid', password_hash='synthetic', display_name='Other bound owner'))
        await db.flush()
        db.add(Agent(id=target_agent, tenant_id=target_tenant, name='Other card employee', role_description='Other work',
            creator_id=target_user, owner_user_id=target_user, sponsor_user_id=target_user))
        await db.flush()
        target_config = ChannelConfig(id=uuid4(), tenant_id=target_tenant, agent_id=target_agent, channel_type='feishu',
            app_id='synthetic-other-card-app', app_secret='synthetic-other-card-secret', is_configured=True)
        db.add(target_config)
        await db.flush()
        await bind_authenticated_self_channel_principal(db, tenant_id=target_tenant, config=target_config,
            provider_subject_id='ou-card-owner', user_id=target_user, actor_user_id=target_user)
        target = ApprovalRequest(id=uuid4(), tenant_id=target_tenant, agent_id=target_agent, action_type='publish',
            requested_by=target_user, status='pending', execution_status='pending', details={})
        db.add(target)
        await db.commit()
    monkeypatch.setattr('app.database.async_session', app_user_sessionmaker)
    async def no_notify(*args, **kwargs):
        return None
    monkeypatch.setattr('app.services.notification_service.send_notification', no_notify)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        await accept_authenticated_channel_event(db, tenant_id=tenant_id, agent_id=agent_id, provider='feishu',
            installation_ref=str(config.id), provider_event_id=uuid4().hex, handler_key='feishu.card_action',
            body=_card_body(target.id), metadata={'feishu_message_scope': feishu_message_scope(config)})
    service = ChannelIngressInboxService(session_factory=app_user_sessionmaker, max_attempts=1)
    await service.drain_once(worker_id='card-origin-proof')
    async with owner_sessionmaker() as db:
        saved = await db.get(ApprovalRequest, target.id)
        assert saved.status == 'pending' and saved.resolved_by is None
        assert not list(await db.scalars(select(AuditLog).where(AuditLog.agent_id == target_agent,
            AuditLog.action == 'approval_approved')))
    assert user_id != target_user


@pytest.mark.parametrize('mode', ['sdk_without_http_key', 'encrypted_webhook', 'tampered_payload'])
async def test_current_card_ingress_proves_the_exact_callback_before_decision(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, mode,
):
    from dataclasses import replace
    from app.api.feishu import router
    from app.database import pin_rls_tenant_context
    from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
    from app.services.channel_ingress_inbox import ChannelIngressInboxService, accept_authenticated_channel_event
    from app.services.channel_provider_receipts import feishu_message_scope

    tenant_id, user_id, agent_id, config, approval_id = await _card_world(owner_sessionmaker)
    async def no_notify(*args, **kwargs):
        return None
    monkeypatch.setattr('app.services.notification_service.send_notification', no_notify)
    monkeypatch.setattr('app.database.async_session', app_user_sessionmaker)
    body = _card_body(approval_id)
    other_id = None
    if mode == 'sdk_without_http_key':
        async with owner_sessionmaker() as db:
            current = await db.get(ChannelConfig, config.id)
            current.encrypt_key = current.verification_token = None
            await db.commit()
    service = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    if mode == 'encrypted_webhook':
        from tests.api.test_feishu_webhook_security import _encrypt_feishu_payload

        event = {'header': {'event_type': 'card.action.trigger', 'event_id': uuid4().hex, 'app_id': config.app_id}, 'event': body}
        raw = json.dumps({'encrypt': _encrypt_feishu_payload(config.encrypt_key, event)}, separators=(',', ':'))
        timestamp, nonce = '1792684000', 'synthetic-encrypted-card'
        headers = {'Content-Type': 'application/json', 'X-Lark-Request-Timestamp': timestamp,
            'X-Lark-Request-Nonce': nonce,
            'X-Lark-Signature': hashlib.sha256((timestamp + nonce + config.encrypt_key + raw).encode()).hexdigest()}
        app = FastAPI()
        app.add_middleware(TenantMiddleware)
        app.include_router(router, prefix='/api')
        async def database():
            async with app_user_sessionmaker() as db:
                yield db
        app.dependency_overrides[get_db] = database
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://encrypted-card') as client:
            received = await client.post(f'/api/channel/feishu/{agent_id}/webhook', content=raw, headers=headers)
            assert received.status_code == 200 and received.json()['msg'] == 'accepted', received.text
    else:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            await accept_authenticated_channel_event(db, tenant_id=tenant_id, agent_id=agent_id,
                provider='feishu', installation_ref=str(config.id), provider_event_id=uuid4().hex,
                handler_key='feishu.card_action', body=body, metadata={'feishu_message_scope': feishu_message_scope(config)})
    items = await service.claim_batch(worker_id='card-exact-source')
    assert len(items) == 1
    item = items[0]
    if mode == 'tampered_payload':
        async with owner_sessionmaker() as db:
            other = ApprovalRequest(id=uuid4(), tenant_id=tenant_id, agent_id=agent_id, action_type='publish',
                requested_by=user_id, status='pending', execution_status='pending', details={})
            db.add(other)
            await db.commit()
            other_id = other.id
        item = replace(item, payload={**item.payload, 'body': _card_body(other_id)})
    result = await dispatch_channel_ingress_event(item)
    assert await service._mark_processed(item=item, worker_id='card-exact-source', receipt=result)
    async with owner_sessionmaker() as db:
        saved = await db.get(ApprovalRequest, approval_id)
        if mode == 'tampered_payload':
            assert saved.status == 'pending' and (await db.get(ApprovalRequest, other_id)).status == 'pending'
            assert result['status'] == 'denied' and result['reason_code'] == 'feishu_card_payload_changed'
        else:
            assert saved.status == 'approved' and saved.resolved_by == user_id
        audits = list(await db.scalars(select(AuditLog).where(AuditLog.agent_id == agent_id, AuditLog.action == 'approval_approved')))
        assert len(audits) == (0 if mode == 'tampered_payload' else 1)
    assert (await service.drain_once(worker_id='card-no-replay'))['claimed'] == 0
