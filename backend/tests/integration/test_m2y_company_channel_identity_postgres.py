"""Company sender authority requires a self-issued challenge and authenticated transport."""

import base64
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.api import tenant_channels
from app.core.security import get_current_user
from app.database import get_db
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.organization_identity import OrgMemberUserLink
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run


@pytest.fixture
async def company_messaging(owner_sessionmaker, app_user_sessionmaker, tmp_path):
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    async with owner_sessionmaker() as db:
        user = await db.get(User, seed['user_id'])
        admin = User(id=uuid4(), tenant_id=seed['tenant_id'], username=uuid4().hex,
            email=f'{uuid4().hex}@fixture.test', password_hash='synthetic', display_name='Setup admin', role='org_admin')
        db.add(admin)
        await db.flush()
        configs = {}
        for provider in ('wecom', 'dingtalk'):
            config = TenantChannelConfig(id=uuid4(), tenant_id=seed['tenant_id'], channel_type=provider,
                app_id=f'synthetic-{provider}-{uuid4().hex}', app_secret=f'synthetic-{provider}-secret',
                verification_token='synthetic-callback-token', encrypt_key=base64.b64encode(b'w' * 32).decode().rstrip('='),
                extra_config={'wecom_agent_id': '100001', 'corp_id': 'synthetic-company', 'messaging_enabled': True})
            db.add(config)
            await db.flush()
            identity = IdentityProvider(id=uuid4(), tenant_id=seed['tenant_id'], provider_type=provider,
                name=provider, is_active=True, sso_login_enabled=False,
                config={'connection_id': str(config.id), 'app_id': config.app_id})
            db.add(identity)
            await db.flush()
            member = OrgMember(id=uuid4(), tenant_id=seed['tenant_id'], provider_id=identity.id,
                external_id='member-one', name='Verified directory subject', status='active')
            db.add(member)
            await db.flush()
            db.add(OrgMemberUserLink(tenant_id=seed['tenant_id'], org_member_id=member.id, user_id=user.id,
                provider_id=identity.id, subject_kind='provider_user_id', match_subject='member-one', status='active',
                created_by_user_id=admin.id, evidence_json={'source': 'manual', 'observed_at': datetime.now(timezone.utc).isoformat()}))
            configs[provider] = config
        await db.commit()
    actor = {'user': user}
    async def database():
        async with app_user_sessionmaker() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    app = FastAPI()
    app.include_router(tenant_channels.router)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: actor['user']
    return {**seed, 'app': app, 'actor': actor, 'configs': configs, 'admin': admin}


async def test_directory_link_cannot_bind_channel_without_member_challenge(company_messaging, owner_sessionmaker):
    seed = company_messaging
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
        status = await api.get('/tenant-channels/wecom/identity', params=scope)
        assert status.status_code == 200, status.text
        assert status.json()['state'] == 'verification_required', status.text
        response = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        assert response.status_code == 200, response.text
        assert response.json()['command'].startswith('/hive-connect ')
        seed['actor']['user'] = seed['admin']
        denied = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        assert denied.status_code == 409, denied.text
    from app.models.external_principal import ExternalPrincipal
    async with owner_sessionmaker() as db:
        assert not list(await db.scalars(select(ExternalPrincipal).where(ExternalPrincipal.tenant_id == seed['tenant_id'])))


def company_wecom_request(config, *, text, subject='member-one', recipient=None, agent_id='100001', message_id='binding-message'):
    import xml.etree.ElementTree as ET
    from app.api.wecom import _encrypt_msg, _verify_signature
    plain = ET.Element('xml')
    for key, value in {'ToUserName': recipient or config.app_id, 'FromUserName': subject, 'AgentID': agent_id,
        'MsgType': 'text', 'MsgId': message_id, 'Content': text}.items():
        ET.SubElement(plain, key).text = value
    encrypted = _encrypt_msg(config.encrypt_key, ET.tostring(plain, encoding='unicode'), recipient or config.app_id)
    outer = ET.Element('xml')
    ET.SubElement(outer, 'Encrypt').text = encrypted
    params = {'timestamp': '1792400000', 'nonce': 'fixture-nonce',
        'msg_signature': _verify_signature(config.verification_token, '1792400000', 'fixture-nonce', encrypted)}
    return ET.tostring(outer), params


async def test_signed_recipient_and_matching_subject_spend_once_and_require_fresh_reconnect(
    company_messaging, owner_sessionmaker, app_user_sessionmaker,
):
    from app.models.external_principal import ExternalPrincipal
    from app.models.tenant_channel_config import CompanyChannelBindingChallenge
    from app.services.external_principal_service import load_external_runtime_actor
    from app.database import pin_rls_tenant_context

    seed = company_messaging
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    url = f"/channel/wecom/tenant/{seed['tenant_id']}/webhook"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        code = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        assert code.status_code == 200, code.text
        command = code.json()['command']
        config = seed['configs']['wecom']
        for case, kwargs in [('recipient', {'recipient': 'wrong-company'}), ('application', {'agent_id': '200002'})]:
            body, params = company_wecom_request(config, text=command, **kwargs)
            rejected = await api.post(url, content=body, params=params)
            assert rejected.status_code == 403, (case, rejected.text)
        body, params = company_wecom_request(config, text=command, subject='different-subject')
        assert (await api.post(url, content=body, params=params)).status_code == 200
        async with owner_sessionmaker() as db:
            assert not list(await db.scalars(select(ExternalPrincipal).where(ExternalPrincipal.tenant_id == seed['tenant_id'])))
            proof = await db.scalar(select(CompanyChannelBindingChallenge).where(CompanyChannelBindingChallenge.user_id == seed['user_id']))
            assert proof.consumed_at is None
            import hashlib
            assert proof.token_sha256 == hashlib.sha256(command.split(' ')[1].encode()).hexdigest()
            assert command not in repr(vars(proof))
        body, params = company_wecom_request(config, text=command)
        assert (await api.post(url, content=body, params=params)).status_code == 200
        assert (await api.post(url, content=body, params=params)).status_code == 200
        status = await api.get('/tenant-channels/wecom/identity', params=scope)
        assert status.json()['state'] == 'bound', status.text
        principal_id = status.json()['principal_id']
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            actor = await load_external_runtime_actor(db, tenant_id=seed['tenant_id'], principal_id=principal_id, expected_user_id=seed['user_id'])
            assert actor.authority_bound and actor.id == seed['user_id']
        disconnected = await api.post('/tenant-channels/wecom/identity/disconnect', json={**scope, 'principal_id': principal_id})
        assert disconnected.status_code == 200, disconnected.text
        assert (await api.post(url, content=body, params=params)).status_code == 200
        status = await api.get('/tenant-channels/wecom/identity', params=scope)
        assert status.json()['state'] == 'reconnect_required', status.text
        code = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        body, params = company_wecom_request(config, text=code.json()['command'], message_id='fresh-binding')
        assert (await api.post(url, content=body, params=params)).status_code == 200
        assert (await api.get('/tenant-channels/wecom/identity', params=scope)).json()['state'] == 'bound'


@pytest.fixture
async def company_runtime(company_messaging, owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    import sys
    import app.database as database
    from app.config import get_settings
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.user_assistant_binding import UserAssistantBinding
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.secrets_provider import get_secrets_provider
    seed = company_messaging
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, '__name__', '').startswith('app.'):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), 'AGENT_DATA_DIR', str(seed['data_root']))
    monkeypatch.setattr(agent_tools, 'WORKSPACE_ROOT', seed['data_root'])
    monkeypatch.setattr('app.tools.workspace.WORKSPACE_ROOT', seed['data_root'])
    monkeypatch.setattr(agent_tools, '_TOOL_RUNTIME_SERVICE', None)
    monkeypatch.setattr(agent_context, 'PERSISTENT_DATA', seed['data_root'])
    monkeypatch.setattr(agent_context, 'TOOL_WORKSPACE', seed['data_root'])
    monkeypatch.setattr(agent_manager, 'settings', get_settings())
    monkeypatch.setattr('random.uniform', lambda *_: 0)
    (seed['agent_root'] / 'workspace').rmdir()
    seed['agent_root'].rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed['agent_id'])
        agent.default_session_permission_mode = 'bypassPermissions'
        await AgentManager().initialize_agent_files(db, agent, personality='Company assistant', original_author_id=seed['user_id'])
        db.add(CapabilityPolicy(tenant_id=seed['tenant_id'], capability='workspace.file.write', allowed=True, requires_approval=False))
        db.add(UserAssistantBinding(tenant_id=seed['tenant_id'], user_id=seed['user_id'], agent_id=agent.id,
            status='active', source='explicit_selection', requester_user_id=seed['user_id'], owner_user_id_at_bind=seed['user_id']))
        (await db.get(RuntimeTask, seed['run_id'])).status = 'completed'
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt('synthetic-company-model')
        model.max_input_tokens = 256_000
        await db.commit()
    return seed


def company_dingtalk_body(config, text, message_id):
    return {'senderCorpId': 'synthetic-company', 'chatbotCorpId': 'synthetic-company', 'robotCode': config.app_id,
        'senderStaffId': 'member-one', 'conversationType': '1', 'conversationId': 'synthetic-private', 'msgtype': 'text',
        'msgId': message_id, 'text': {'content': text},
        'sessionWebhook': 'https://oapi.dingtalk.com/robot/sendBySession?session=synthetic-session-secret'}


@pytest.mark.parametrize('provider,outcome', [('wecom', 'success'), ('dingtalk', 'success'),
    ('wecom', 'rejected'), ('dingtalk', 'unknown')])
async def test_authenticated_private_work_uses_native_tools_and_original_outbox(
    company_runtime, owner_sessionmaker, app_user_sessionmaker, monkeypatch, provider, outcome,
):
    import json
    from uuid import UUID
    from app.database import pin_rls_tenant_context
    from app.models.audit import ChatMessage
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.user_assistant_binding import UserAssistantBinding
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
    from app.services.channel_ingress_inbox import ChannelIngressInboxService, channel_installation_ref
    from app.services.company_dingtalk_stream import accept_company_dingtalk_message
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient
    seed, calls = company_runtime, []
    config = seed['configs'][provider]
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        async def inbound(text, message_id):
            if provider == 'wecom':
                body, params = company_wecom_request(config, text=text, message_id=message_id)
                response = await api.post(f"/channel/wecom/tenant/{seed['tenant_id']}/webhook", content=body, params=params)
                assert response.status_code == 200, response.text
            else:
                return await accept_company_dingtalk_message(tenant_id=seed['tenant_id'], config_id=config.id,
                    revision=tenant_channels._config_revision(config), installation_ref=channel_installation_ref(config, fallback=''),
                    body=company_dingtalk_body(config, text, message_id))
        challenge = await api.post(f'/tenant-channels/{provider}/identity/challenge', json=scope)
        assert challenge.status_code == 200, challenge.text
        await inbound(challenge.json()['command'], 'verify-self')
        status = await api.get(f'/tenant-channels/{provider}/identity', params=scope)
        assert status.json()['state'] == 'bound', status.text
        await inbound('Write the company report, read the file and report its contents.', 'company-work')
        inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
        claimed = next(item for item in await inbox.claim_batch(worker_id='company', limit=50)
            if item.provider_event_id == 'company-work' and item.tenant_id == seed['tenant_id'])
        # Once accepted, removing the default selection must not retarget a replay.
        async with owner_sessionmaker() as db:
            binding = await db.scalar(select(UserAssistantBinding).where(UserAssistantBinding.user_id == seed['user_id']))
            await db.delete(binding)
            await db.commit()
        await inbound('Write the company report, read the file and report its contents.', 'company-work')
    admitted = await dispatch_channel_ingress_event(claimed)
    recovered = await dispatch_channel_ingress_event(claimed)
    assert UUID(recovered['runtime_task_id']) == UUID(admitted['runtime_task_id'])
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        session = await db.get(ChatSession, UUID(admitted['session_id']))
        assert session.user_id == seed['user_id'] and session.agent_id == seed['agent_id']
        assert str(session.external_principal_id) == admitted['external_principal_id']
        assert session.source_channel == provider and session.audience_kind != 'group'
        messages = list(await db.scalars(select(ChatMessage).where(ChatMessage.source_ingress_event_id == claimed.id)))
        assert len(messages) == 1 and messages[0].user_id == seed['user_id']
    script = ScriptedLLMClient([
        {'tool_calls': [{'id': 'company-write', 'type': 'function', 'function': {'name': 'write_file',
            'arguments': json.dumps({'path': 'workspace/company-report.md', 'content': 'COMPANY_RESULT'})}}], 'finish_reason': 'tool_calls'},
        {'tool_calls': [{'id': 'company-read', 'type': 'function', 'function': {'name': 'read_file',
            'arguments': json.dumps({'path': 'workspace/company-report.md'})}}], 'finish_reason': 'tool_calls'},
        {'content': 'The company report contains COMPANY_RESULT.', 'finish_reason': 'stop'},
    ])
    monkeypatch.setattr('app.runtime.invoker.create_llm_client', lambda **_kwargs: script)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        claims = await RuntimeTaskClaimService(db=db, worker_id='company-worker', lease_seconds=600).claim_available()
        claim = next(item for item in claims if str(item.id) == admitted['runtime_task_id'])
        await db.commit()
    fence = set_runtime_task_fence(task_id=claim.id, worker_id='company-worker', claim_version=claim.claim_version)
    try:
        await execute_web_chat_run(claim.id)
    finally:
        reset_runtime_task_fence(fence)
    assert len(script.requests) == 3 and 'COMPANY_RESULT' in json.dumps(script.requests[-1]['messages'])
    assert (seed['agent_root'] / 'workspace/company-report.md').read_text() == 'COMPANY_RESULT'
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        task = await db.get(RuntimeTask, claim.id)
        assert task.status == 'completed' and task.root_user_id == seed['user_id'], (task.status, task.result_summary)
        outbox = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == task.id))
        assert outbox is not None and outbox.channel_config_id == config.id
        assert outbox.delivery_target_json['tenant_channel_config_id'] == str(config.id)
    factory = httpx.AsyncClient
    def provider_request(request):
        calls.append(request)
        if request.url.path.endswith('/gettoken'):
            return httpx.Response(200, json={'errcode': 0, 'access_token': 'synthetic-token'})
        if outcome == 'unknown':
            raise httpx.ReadTimeout('synthetic response lost', request=request)
        return httpx.Response(200, json={'errcode': 0 if outcome == 'success' else 40014, 'msgid': 'synthetic-receipt'})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(transport=httpx.MockTransport(provider_request), **kwargs))
    service = ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker)
    await service.drain_once(worker_id='company-results', limit=50)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        row = await db.get(ChannelDeliveryOutbox, outbox.id)
        part = row.delivery_receipts_json['text']
        assert part['state'] == {'success': 'delivered', 'unknown': 'unknown', 'rejected': 'failed'}[outcome], part
        if outcome != 'success':
            assert row.status == ('needs_reconciliation' if outcome == 'unknown' else 'dead_letter')
        if provider == 'wecom':
            payload = json.loads(next(r.content for r in calls if r.method == 'POST'))
            assert payload['touser'] == 'member-one' and payload['agentid'] == 100001
        else:
            assert len(calls) == 1 and calls[0].url.host == 'oapi.dingtalk.com'
    before = len(calls)
    await service.drain_once(worker_id='company-results', limit=50)
    assert len(calls) == before



async def test_owned_sdk_process_authenticates_commits_and_stops_on_settings_change(
    company_runtime, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    import asyncio
    import json
    import multiprocessing
    from functools import partial
    from pathlib import Path
    from app.models.channel_ingress_event import ChannelIngressEvent
    from app.services import company_dingtalk_stream as stream
    from tests.company_dingtalk_sdk_fixture import controlled_sdk_worker
    seed = company_runtime
    config = seed['configs']['dingtalk']
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        response = await api.post('/tenant-channels/dingtalk/identity/challenge', json=scope)
        assert response.status_code == 200, response.text
        command = response.json()['command']
        wrong = company_dingtalk_body(config, command, 'wrong-company')
        wrong['senderCorpId'] = 'another-company'
        messages = [wrong, company_dingtalk_body(config, command, 'verify-sdk'),
            company_dingtalk_body(config, 'Perform the company assignment', 'sdk-work')]
        finished = multiprocessing.get_context('spawn').Event()
        manager = stream.CompanyDingTalkStreamManager(worker_target=partial(controlled_sdk_worker, messages, finished))
        contender = stream.CompanyDingTalkStreamManager(worker_target=partial(controlled_sdk_worker, messages, finished))
        monkeypatch.setattr(stream, 'company_dingtalk_stream_manager', manager)
        try:
            await manager.reconcile(config)
            record = manager.workers[config.id]
            home, process = Path(record['home'].name), record['process']
            assert await asyncio.to_thread(finished.wait, 15), manager.status()
            assert json.loads((home / 'acks.json').read_text()) == [[500, 'retry'], [200, 'accepted'], [200, 'accepted']]
            assert manager.status()[str(config.id)]['state'] == 'receiving'
            await contender.reconcile(config)
            assert not contender.workers and contender.status()[str(config.id)]['state'] == 'managed_elsewhere'
            async with owner_sessionmaker() as db:
                stored = await db.scalar(select(ChannelIngressEvent).where(ChannelIngressEvent.tenant_id == seed['tenant_id'],
                    ChannelIngressEvent.provider_event_id == 'sdk-work'))
                assert stored is not None and stored.status == 'received'
                raw = await db.scalar(text('SELECT payload_json FROM channel_ingress_events WHERE id=:id'), {'id': stored.id})
                assert 'synthetic-session-secret' not in str(raw)
                assert stored.payload_json['body']['delivery_target']['session_webhook'].endswith('synthetic-session-secret')
            previous_home = home
            previous_pid = process.pid
            process.kill()
            await asyncio.to_thread(process.join, 5)
            finished.clear()
            await manager.reconcile_once()
            record = manager.workers[config.id]
            home, process = Path(record['home'].name), record['process']
            assert process.pid != previous_pid and not previous_home.exists()
            assert await asyncio.to_thread(finished.wait, 15), manager.status()
            async with owner_sessionmaker() as db:
                events = list(await db.scalars(select(ChannelIngressEvent).where(ChannelIngressEvent.tenant_id == seed['tenant_id'],
                    ChannelIngressEvent.provider_event_id == 'sdk-work')))
                assert len(events) == 1
            seed['actor']['user'] = seed['admin']
            response = await api.put('/tenant-channels/dingtalk', json={**scope, 'expected_user_id': str(seed['admin'].id),
                'app_id': config.app_id, 'expected_revision': tenant_channels._config_revision(config),
                'extra_config': {**config.extra_config, 'messaging_enabled': False}})
            assert response.status_code == 200, response.text
            from app.database import _after_commit_tasks
            await asyncio.wait_for(asyncio.gather(*list(_after_commit_tasks)), 15)
            assert not process.is_alive() and not home.exists()
            assert config.id not in manager.workers and manager.status()[str(config.id)]['state'] == 'disabled'
            # A stale reconciliation callback must re-read the current disabled configuration.
            await manager.reconcile(config)
            assert not manager.workers
            await contender.reconcile_once()
            assert not contender.workers
            from app.services.dingtalk_stream import dingtalk_stream_manager
            discovered = await dingtalk_stream_manager._configured_companies()
            assert config.id not in {item.id for item in discovered}
            assert all(item.extra_config.get('messaging_enabled') is True for item in discovered)
        finally:
            await manager.stop_all()
            await contender.stop_all()


async def test_company_bot_activation_rejects_incomplete_namespace_and_application_reuse(
    company_messaging, owner_sessionmaker, tmp_path,
):
    from app.models.channel_config import ChannelConfig
    seed = company_messaging
    seed['actor']['user'] = seed['admin']
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['admin'].id)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        for provider, extra in [('wecom', {'messaging_enabled': True, 'wecom_agent_id': '0'}),
            ('wecom', {'messaging_enabled': True, 'wecom_agent_id': 'one'}),
            ('dingtalk', {'messaging_enabled': True}), ('dingtalk', {'messaging_enabled': 'true'})]:
            response = await api.put(f'/tenant-channels/{provider}', json={**scope,
                'app_id': seed['configs'][provider].app_id, 'extra_config': extra})
            assert response.status_code == 422, response.text
        async with owner_sessionmaker() as db:
            employee = ChannelConfig(id=uuid4(), tenant_id=seed['tenant_id'], agent_id=seed['agent_id'], channel_type='dingtalk',
                app_id=seed['configs']['dingtalk'].app_id, app_secret='synthetic-employee-secret', is_configured=True)
            db.add(employee)
            await db.commit()
        response = await api.put('/tenant-channels/dingtalk', json={**scope, 'app_id': employee.app_id})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'company_dingtalk_application_in_use'
        async with owner_sessionmaker() as db:
            await db.delete(await db.get(ChannelConfig, employee.id))
            await db.commit()
        other = await _seed_run(owner_sessionmaker, tmp_path / 'other-company')
        seed['actor']['user'] = None
        async with owner_sessionmaker() as db:
            admin = await db.get(User, other['user_id'])
            admin.role = 'org_admin'
            await db.commit()
            seed['actor']['user'] = admin
        response = await api.put('/tenant-channels/dingtalk', json={'expected_tenant_id': str(other['tenant_id']),
            'expected_user_id': str(admin.id), 'app_id': seed['configs']['dingtalk'].app_id,
            'app_secret': 'synthetic-other-secret', 'extra_config': {'messaging_enabled': True, 'corp_id': 'synthetic-company'}})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'company_dingtalk_application_in_use'


async def test_revoked_installation_is_visible_and_cannot_redeem_a_new_proof(company_messaging, owner_sessionmaker):
    from app.services.channel_ingress_inbox import channel_installation_ref
    from app.services.external_principal_service import revoke_external_principals_for_installation
    seed = company_messaging
    config = seed['configs']['wecom']
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        issued = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        body, params = company_wecom_request(config, text=issued.json()['command'])
        url = f"/channel/wecom/tenant/{seed['tenant_id']}/webhook"
        assert (await api.post(url, content=body, params=params)).status_code == 200
        async with owner_sessionmaker() as db:
            changed = await revoke_external_principals_for_installation(db, tenant_id=seed['tenant_id'], provider='wecom',
                installation_ref=channel_installation_ref(config, fallback=''), actor_user_id=seed['admin'].id,
                reason='Installation retired by scoped administrator')
            assert changed == 1
            await db.commit()
        status = await api.get('/tenant-channels/wecom/identity', params=scope)
        assert status.status_code == 200 and status.json()['state'] == 'installation_revoked', status.text
        fresh = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        assert fresh.status_code == 409 and fresh.json()['detail']['code'] == 'company_channel_installation_revoked'
        assert (await api.post(url, content=body, params=params)).status_code == 200


async def test_parent_sigkill_ends_sdk_and_replacement_keeps_one_durable_event(
    company_runtime, owner_sessionmaker, app_user_sessionmaker, tmp_path,
):
    import asyncio
    import multiprocessing
    import os
    import shutil
    import signal
    from functools import partial
    from multiprocessing.connection import wait
    from pathlib import Path
    from app.models.channel_ingress_event import ChannelIngressEvent
    from app.services.company_dingtalk_stream import CompanyDingTalkStreamManager
    from tests.company_dingtalk_sdk_fixture import controlled_owner, controlled_sdk_worker
    seed = company_runtime
    config = seed['configs']['dingtalk']
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        issued = await api.post('/tenant-channels/dingtalk/identity/challenge', json=scope)
        assert issued.status_code == 200, issued.text
    messages = [company_dingtalk_body(config, issued.json()['command'], 'parent-verification'),
        company_dingtalk_body(config, 'Work survives the transport owner', 'parent-work')]
    context = multiprocessing.get_context('spawn')
    finished = context.Event()
    reports, child_reports = context.Pipe(duplex=False)
    database_url = app_user_sessionmaker.kw['bind'].url.render_as_string(hide_password=False)
    owner = context.Process(target=controlled_owner, args=(database_url, seed['tenant_id'], config.id, messages, finished, child_reports))
    replacement_finished = context.Event()
    replacement = CompanyDingTalkStreamManager(worker_target=partial(controlled_sdk_worker, messages, replacement_finished))
    report, sentinel = None, None
    try:
        owner.start()
        child_reports.close()
        assert await asyncio.to_thread(reports.poll, 20), 'Owner did not report its owned SDK'
        report = reports.recv()
        assert 'error_class' not in report, report
        sentinel = report['sentinel'].detach()
        assert await asyncio.to_thread(finished.wait, 15)
        owner.kill()
        await asyncio.to_thread(owner.join, 5)
        assert not owner.is_alive()
        assert await asyncio.to_thread(wait, [sentinel], 5), 'SDK outlived its killed owner'
        await replacement.reconcile(config)
        assert config.id in replacement.workers, replacement.status()
        assert replacement.workers[config.id]['process'].pid != report['child_pid']
        assert await asyncio.to_thread(replacement_finished.wait, 15), replacement.status()
        async with owner_sessionmaker() as db:
            events = list(await db.scalars(select(ChannelIngressEvent).where(ChannelIngressEvent.tenant_id == seed['tenant_id'],
                ChannelIngressEvent.provider_event_id == 'parent-work')))
            assert len(events) == 1 and events[0].status == 'received'
    finally:
        if owner.is_alive():
            owner.kill()
        await asyncio.to_thread(owner.join, 5)
        await replacement.stop_all()
        if sentinel is not None:
            if not await asyncio.to_thread(wait, [sentinel], 0):
                os.kill(report['child_pid'], signal.SIGTERM)
                assert await asyncio.to_thread(wait, [sentinel], 5)
            os.close(sentinel)
        reports.close()
        if report is not None and 'home' in report:
            home = Path(report['home'])
            assert home.name.startswith('hive-company-dingtalk-') and not home.is_symlink()
            if home.exists():
                shutil.copyfile(home / 'acks.json', tmp_path / 'orphan-sdk-acks.json')
                shutil.rmtree(home)


async def test_expired_challenge_never_creates_sender_authority(company_messaging, owner_sessionmaker):
    from datetime import timedelta
    from app.models.external_principal import ExternalPrincipal
    from app.models.tenant_channel_config import CompanyChannelBindingChallenge
    seed = company_messaging
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        issued = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        assert issued.status_code == 200, issued.text
        async with owner_sessionmaker() as db:
            proof = await db.scalar(select(CompanyChannelBindingChallenge).where(CompanyChannelBindingChallenge.user_id == seed['user_id']))
            proof.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        body, params = company_wecom_request(seed['configs']['wecom'], text=issued.json()['command'])
        received = await api.post(f"/channel/wecom/tenant/{seed['tenant_id']}/webhook", content=body, params=params)
        assert received.status_code == 200, received.text
    async with owner_sessionmaker() as db:
        assert (await db.get(CompanyChannelBindingChallenge, proof.id)).consumed_at is None
        assert await db.scalar(select(ExternalPrincipal.id).where(ExternalPrincipal.tenant_id == seed['tenant_id'])) is None


async def test_directory_reassignment_cannot_inherit_prior_holder_or_private_work(company_messaging, owner_sessionmaker):
    from app.models.external_principal import ExternalPrincipal
    seed = company_messaging
    original = seed['actor']['user']
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(original.id)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        issued = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        body, params = company_wecom_request(seed['configs']['wecom'], text=issued.json()['command'])
        assert (await api.post(f"/channel/wecom/tenant/{seed['tenant_id']}/webhook", content=body, params=params)).status_code == 200
        async with owner_sessionmaker() as db:
            principal = await db.scalar(select(ExternalPrincipal).where(ExternalPrincipal.tenant_id == seed['tenant_id']))
            previous = await db.scalar(select(OrgMemberUserLink).where(OrgMemberUserLink.user_id == original.id,
                OrgMemberUserLink.provider_id == (await db.scalar(select(IdentityProvider.id).where(
                    IdentityProvider.tenant_id == seed['tenant_id'], IdentityProvider.provider_type == 'wecom')))))
            previous.status = 'revoked'
            await db.flush()
            db.add(OrgMemberUserLink(tenant_id=seed['tenant_id'], org_member_id=previous.org_member_id, user_id=seed['admin'].id,
                provider_id=previous.provider_id, subject_kind='provider_user_id', match_subject=previous.match_subject,
                status='active', created_by_user_id=seed['admin'].id,
                evidence_json={'source': 'manual', 'observed_at': datetime.now(timezone.utc).isoformat()}))
            await db.commit()
        seed['actor']['user'] = seed['admin']
        new_scope = {**scope, 'expected_user_id': str(seed['admin'].id)}
        status = await api.get('/tenant-channels/wecom/identity', params=new_scope)
        assert status.status_code == 200 and status.json()['state'] == 'identity_history_conflict', status.text
        assert status.json()['principal_id'] is None and status.json()['disconnectable'] == []
        denied = await api.post('/tenant-channels/wecom/identity/challenge', json=new_scope)
        assert denied.status_code == 409 and denied.json()['detail']['code'] == 'company_channel_identity_history_conflict'
        denied_unlink = await api.post('/tenant-channels/wecom/identity/disconnect', json={**new_scope, 'principal_id': str(principal.id)})
        assert denied_unlink.status_code == 403
        seed['actor']['user'] = original
        unlinked = await api.post('/tenant-channels/wecom/identity/disconnect', json={**scope, 'principal_id': str(principal.id)})
        assert unlinked.status_code == 200, unlinked.text
        seed['actor']['user'] = seed['admin']
        denied = await api.post('/tenant-channels/wecom/identity/challenge', json=new_scope)
        assert denied.status_code == 409 and denied.json()['detail']['code'] == 'company_channel_identity_history_conflict'


async def test_disconnect_invalidates_preexisting_unspent_challenges(company_messaging, owner_sessionmaker):
    from app.models.external_principal import ExternalPrincipal
    seed = company_messaging
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        first = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        body, params = company_wecom_request(seed['configs']['wecom'], text=first.json()['command'])
        url = f"/channel/wecom/tenant/{seed['tenant_id']}/webhook"
        assert (await api.post(url, content=body, params=params)).status_code == 200
        pending = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        assert pending.status_code == 200, pending.text
        identity = (await api.get('/tenant-channels/wecom/identity', params=scope)).json()
        unlinked = await api.post('/tenant-channels/wecom/identity/disconnect', json={**scope, 'principal_id': identity['principal_id']})
        assert unlinked.status_code == 200, unlinked.text
        retried = await api.post('/tenant-channels/wecom/identity/disconnect', json={**scope, 'principal_id': identity['principal_id']})
        assert retried.status_code == 200 and retried.json()['authority_bound'] is False
        stale_body, stale_params = company_wecom_request(seed['configs']['wecom'], text=pending.json()['command'], message_id='stale-unspent')
        assert (await api.post(url, content=stale_body, params=stale_params)).status_code == 200
        async with owner_sessionmaker() as db:
            principal = await db.scalar(select(ExternalPrincipal).where(ExternalPrincipal.tenant_id == seed['tenant_id']))
            assert principal.linked_user_id is None, 'Disconnect must revoke pending authorizations too'
        fresh = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        body, params = company_wecom_request(seed['configs']['wecom'], text=fresh.json()['command'], message_id='fresh-after-disconnect')
        assert (await api.post(url, content=body, params=params)).status_code == 200
        assert (await api.get('/tenant-channels/wecom/identity', params=scope)).json()['state'] == 'bound'
