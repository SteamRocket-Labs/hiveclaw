"""Smart Bot identities, encrypted inputs and single-use replies stay in one work."""

import base64
import hashlib
import json
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text

from app.api.wecom import _encrypt_msg, _pad, _verify_signature
from app.database import pin_rls_tenant_context
from app.models.tenant_channel_config import TenantChannelConfig
from tests.integration import test_m2aa_company_attachments_postgres as _files
from tests.integration import test_m2y_company_channel_identity_postgres as _company

company_messaging = _company.company_messaging
company_runtime = _company.company_runtime


async def _configure(seed, factory, mode='encrypted'):
    async with factory() as db:
        config = await db.get(TenantChannelConfig, seed['configs']['wecom'].id)
        config.extra_config = {**config.extra_config, 'wecom_transport': 'smart_bot',
            'wecom_bot_id': 'synthetic-company-bot', 'wecom_bot_userid_mode': mode}
        await db.commit()
        return config


def _request(config, *, content='', event='smartbot-binding', subject='encrypted-member', **overrides):
    body = {'msgid': event, 'aibotid': 'synthetic-company-bot', 'chattype': 'single',
        'from': {'userid': subject}, 'response_url': 'https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=synthetic-private-response',
        'msgtype': 'text', 'text': {'content': content}, **overrides}
    encrypted = _encrypt_msg(config.encrypt_key, json.dumps(body), '')
    params = {'timestamp': '1792400000', 'nonce': 'smartbot-nonce',
        'msg_signature': _verify_signature(config.verification_token, '1792400000', 'smartbot-nonce', encrypted)}
    return {'encrypt': encrypted}, params


async def test_encrypted_bot_file_runs_native_office_and_returns_one_authenticated_work_link(
    company_runtime, app_user_sessionmaker, owner_sessionmaker, monkeypatch,
):
    from Crypto.Cipher import AES
    from app.config import get_settings
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.session_v2 import SessionTurnInput
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
    from app.services.channel_ingress_inbox import ChannelIngressInboxService
    from tests.integration.test_m2z_company_files_postgres import _call
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, calls = company_runtime, []
    config = await _configure(seed, owner_sessionmaker)
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    monkeypatch.setattr(get_settings(), 'PUBLIC_BASE_URL', 'https://work.fixture.test')
    factory, original = httpx.AsyncClient, b'amount\n17\n'
    key = base64.b64decode(config.encrypt_key + '=')
    encrypted_file = AES.new(key, AES.MODE_CBC, key[:16]).encrypt(_pad(original))
    def provider(request):
        calls.append(request)
        if request.url.path.endswith('/gettoken'):
            return httpx.Response(200, json={'errcode': 0, 'access_token': 'synthetic-conversion-token'})
        if request.url.path.endswith('/batch/openuserid_to_userid'):
            assert request.url.params['access_token'] == 'synthetic-conversion-token'
            assert json.loads(request.content) == {'open_userid_list': ['encrypted-member']}
            return httpx.Response(200, json={'errcode': 0,
                'userid_list': [{'open_userid': 'encrypted-member', 'userid': 'member-one'}], 'invalid_open_userid_list': []})
        if request.url.path.endswith('/aibot/response'):
            assert request.url.params['response_code'] == 'synthetic-private-response'
            payload = json.loads(request.content)
            assert payload['msgtype'] == 'markdown'
            assert f"https://work.fixture.test/agents/{seed['agent_id']}/sessions/" in payload['markdown']['content']
            assert 'synthetic-private' not in payload['markdown']['content']
            return httpx.Response(200, json={'errcode': 0, 'errmsg': 'ok'})
        assert request.url.host == 'ww-aibot-img-1258476243.cos.ap-guangzhou.myqcloud.com'
        assert 'authorization' not in request.headers and 'synthetic-conversion-token' not in str(request.url)
        return httpx.Response(200, content=encrypted_file,
            headers={'content-type': 'text/csv', 'content-disposition': 'attachment; filename="amount]report.csv"'})
    async def public(_host, _port):
        return ('93.184.216.34',)
    monkeypatch.setattr('app.services.governed_egress.resolve_public_addresses', public)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(**{**kwargs, 'transport': httpx.MockTransport(provider)}))
    async with factory(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        code = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        assert code.status_code == 200, code.text
        body, params = _request(config, content=code.json()['command'])
        url = f"/channel/wecom/tenant/{seed['tenant_id']}/webhook"
        bound = await api.post(url, json=body, params=params)
        assert bound.status_code == 200, bound.text
        assert (await api.get('/tenant-channels/wecom/identity', params=scope)).json()['state'] == 'bound'
        body, params = _request(config, event='smartbot-file', msgtype='file',
            file={'url': 'https://ww-aibot-img-1258476243.cos.ap-guangzhou.myqcloud.com/input?sign=synthetic-private-file-url'})
        assert (await api.post(url, json=body, params=params)).status_code == 200
        assert (await api.post(url, json=body, params=params)).status_code == 200
    inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    item, = [row for row in await inbox.claim_batch(worker_id='smartbot-input', limit=20) if row.provider_event_id == 'smartbot-file']
    async with owner_sessionmaker() as db:
        raw = await db.scalar(text('SELECT payload_json::text FROM channel_ingress_events WHERE id=:id'), {'id': item.id})
        assert 'synthetic-private-response' not in raw and 'synthetic-private-file-url' not in raw
    admitted = await dispatch_channel_ingress_event(item)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        row = await db.get(SessionTurnInput, UUID(admitted['input_id']))
        attachment, = [part['attachment'] for part in row.content_parts_json if part['type'] == 'attachment_ref']
        path = attachment['path']
        assert path.endswith('/amount]report.csv') and (seed['agent_root'] / path).read_bytes() == original
        assert 'synthetic-private' not in json.dumps(row.content_parts_json)
        from app.services.content_input_receipts import latest_workspace_receipt
        receipt = await latest_workspace_receipt(db, seed['tenant_id'], seed['agent_id'], path)
        assert receipt.content_sha256 == hashlib.sha256(original).hexdigest()
    script = ScriptedLLMClient([
        _call('tool_search', {'query': 'read_file office_document_create office_document_apply'}),
        _call('read_file', {'path': path}),
        _call('office_document_create', {'path': 'workspace/bot-report.xlsx', 'kind': 'xlsx'}),
        _call('office_document_apply', {'path': 'workspace/bot-report.xlsx', 'operations': [
            {'command': 'set', 'path': '/Sheet1/A1', 'props': {'value': '17'}}]}),
        {'content': 'The [Office result](workspace/bot-report.xlsx) is ready.', 'finish_reason': 'stop'},
    ])
    claim = await _files._run_native_work(seed, app_user_sessionmaker, monkeypatch, admitted, script, 'smartbot-worker')
    assert len(script.requests) == 5
    assert any(m.get('role') == 'tool' and 'amount' in str(m.get('content')) and '17' in str(m.get('content'))
        for m in script.requests[2]['messages'])
    assert all('synthetic-private' not in json.dumps(request) for request in script.requests)
    service = ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker)
    await service.drain_once(worker_id='smartbot-return', limit=20)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        outbox = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == claim.id))
        assert outbox.status == 'delivered', (outbox.status, outbox.delivery_receipts_json)
        assert len(outbox.artifact_ids_json) == 1
        assert outbox.delivery_receipts_json['text']['detail']['artifact_delivery'] == 'authenticated_work_link'
        from app.models.session_v2 import SessionModelResult
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == claim.id)))
        assert len(models) == 5
        assert all(model.model_request_snapshot_json['input_bindings']['dependency_coverage']['complete'] for model in models)
    before = len(calls)
    await service.drain_once(worker_id='smartbot-return-again', limit=20)
    assert len(calls) == before
    assert len([r for r in calls if r.url.path.endswith('/aibot/response')]) == 1
    assert not any(r.url.path.endswith('/message/send') or r.url.path.endswith('/media/upload') for r in calls)
    from app.api import files, chat_sessions
    from app.core.security import create_access_token
    from app.models.user import User
    from app.database import get_db
    seed['app'].include_router(files.router)
    seed['app'].include_router(chat_sessions.router)
    seed['app'].dependency_overrides.pop(_company.get_current_user)
    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            yield db
            await db.commit()
    seed['app'].dependency_overrides[get_db] = scoped_db
    async with owner_sessionmaker() as db:
        user = await db.get(User, seed['user_id'])
        jwt = create_access_token(str(user.id), user.role, str(user.tenant_id))
    async with factory(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company',
        headers={'Authorization': 'Bearer ' + jwt}) as web:
        reopened = await web.get(f"/agents/{seed['agent_id']}/sessions/{admitted['session_id']}/workbench")
        assert reopened.status_code == 200, reopened.text
        downloaded = await web.get(f"/agents/{seed['agent_id']}/files/download", params={'path': 'workspace/bot-report.xlsx'})
        assert downloaded.status_code == 200, downloaded.text
    from io import BytesIO
    from zipfile import ZipFile
    from xml.etree import ElementTree
    with ZipFile(BytesIO(downloaded.content)) as archive:
        sheet = ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        assert sheet.find('.//{*}c[@r="A1"]/{*}v').text == '17'


@pytest.mark.parametrize('case', ['signature', 'bot', 'encrypted_raw_id', 'conversion_foreign_id'])
async def test_bot_authentication_and_exact_conversion_cannot_spend_a_member_challenge(
    company_messaging, owner_sessionmaker, monkeypatch, case,
):
    from app.models.external_principal import ExternalPrincipal
    from app.models.tenant_channel_config import CompanyChannelBindingChallenge
    seed, calls = company_messaging, []
    config = await _configure(seed, owner_sessionmaker)
    factory = httpx.AsyncClient
    def provider(request):
        calls.append(request)
        if request.url.path.endswith('/gettoken'):
            return httpx.Response(200, json={'errcode': 0, 'access_token': 'synthetic-token'})
        assert request.url.path.endswith('/batch/openuserid_to_userid')
        raw_id, = json.loads(request.content)['open_userid_list']
        return httpx.Response(200, json={'errcode': 0,
            'userid_list': [{'open_userid': 'different-encrypted-id', 'userid': 'member-one'}] if case == 'conversion_foreign_id' else [],
            'invalid_open_userid_list': [raw_id] if case == 'encrypted_raw_id' else []})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(**{**kwargs, 'transport': httpx.MockTransport(provider)}))
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with factory(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        code = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        assert code.status_code == 200, code.text
        body, params = _request(config, content=code.json()['command'], subject='member-one' if case == 'encrypted_raw_id' else 'encrypted-member',
            **({'aibotid': 'other-bot'} if case == 'bot' else {}))
        if case == 'signature':
            params['msg_signature'] = 'invalid'
        response = await api.post(f"/channel/wecom/tenant/{seed['tenant_id']}/webhook", json=body, params=params)
        assert response.status_code == 403, response.text
    assert len(calls) == (2 if case in {'encrypted_raw_id', 'conversion_foreign_id'} else 0)
    async with owner_sessionmaker() as db:
        assert not list(await db.scalars(select(ExternalPrincipal).where(ExternalPrincipal.tenant_id == seed['tenant_id'])))
        proof = await db.scalar(select(CompanyChannelBindingChallenge).where(CompanyChannelBindingChallenge.user_id == seed['user_id']))
        assert proof.consumed_at is None


async def test_admin_transport_change_requires_fresh_callback_credentials_and_changes_installation(
    company_messaging, owner_sessionmaker,
):
    from app.api.tenant_channels import _config_revision
    from app.services.channel_ingress_inbox import channel_installation_ref
    seed = company_messaging
    config = seed['configs']['wecom']
    previous = channel_installation_ref(config, fallback='')
    seed['actor']['user'] = seed['admin']
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['admin'].id)}
    payload = {**scope, 'app_id': config.app_id, 'expected_revision': _config_revision(config),
        'extra_config': {**config.extra_config, 'wecom_transport': 'smart_bot',
            'wecom_bot_id': 'synthetic-company-bot', 'wecom_bot_userid_mode': 'encrypted'}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        refused = await api.put('/tenant-channels/wecom', json=payload)
        assert refused.status_code == 422, refused.text
        key = base64.b64encode(b'b' * 32).decode().rstrip('=')
        saved = await api.put('/tenant-channels/wecom', json={**payload, 'verification_token': 'synthetic-bot-callback', 'encrypt_key': key})
        assert saved.status_code == 200, saved.text
        assert saved.json()['has_app_secret'] and saved.json()['has_encrypt_key']
        assert 'synthetic-bot-callback' not in saved.text and key not in saved.text
        async with owner_sessionmaker() as db:
            current = await db.get(TenantChannelConfig, config.id)
            assert current.app_secret == config.app_secret and current.encrypt_key == key
            assert channel_installation_ref(current, fallback='') != previous
        encrypted = _encrypt_msg(key, 'verification-with-empty-recipient', '')
        signature = _verify_signature('synthetic-bot-callback', '1792400000', 'verify-url', encrypted)
        verified = await api.get(f"/channel/wecom/tenant/{seed['tenant_id']}/webhook", params={
            'echostr': encrypted, 'timestamp': '1792400000', 'nonce': 'verify-url', 'msg_signature': signature})
        assert verified.status_code == 200 and verified.text == 'verification-with-empty-recipient'


async def _plain_delivery(seed, owner_factory, app_factory):
    from app.models.runtime_task import RuntimeTask
    from app.services.channel_delivery_outbox import enqueue_terminal_delivery_for_task
    from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
    from app.services.channel_ingress_inbox import ChannelIngressInboxService
    config = await _configure(seed, owner_factory, 'plaintext')
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        code = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        assert code.status_code == 200, code.text
        for content, event in [(code.json()['command'], 'plain-proof'), ('Please prepare the report.', 'plain-work')]:
            body, params = _request(config, content=content, subject='member-one', event=event)
            response = await api.post(f"/channel/wecom/tenant/{seed['tenant_id']}/webhook", json=body, params=params)
            assert response.status_code == 200, response.text
    inbox = ChannelIngressInboxService(session_factory=app_factory)
    item, = [row for row in await inbox.claim_batch(worker_id='plain-work', limit=20) if row.provider_event_id == 'plain-work']
    admitted = await dispatch_channel_ingress_event(item)
    async with app_factory() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        task = await db.get(RuntimeTask, UUID(admitted['runtime_task_id']))
        delivery = await enqueue_terminal_delivery_for_task(db, task=task, content='The report is ready.', terminal_status='completed')
        await db.commit()
    return item, admitted, delivery


@pytest.mark.parametrize('case', ['unknown', 'expired', 'revoked', 'bot_replaced', 'two_outputs'])
async def test_bot_one_use_response_preserves_unknown_expiry_and_current_authority(
    company_runtime, app_user_sessionmaker, owner_sessionmaker, monkeypatch, case,
):
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    from app.config import get_settings
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.channel_ingress_event import ChannelIngressEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.external_principal import ExternalPrincipal
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService, enqueue_terminal_delivery_for_task
    seed, calls = company_runtime, []
    item, admitted, delivery = await _plain_delivery(seed, owner_sessionmaker, app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), 'PUBLIC_BASE_URL', 'https://work.fixture.test')
    async with owner_sessionmaker() as db:
        if case == 'expired':
            (await db.get(ChannelIngressEvent, item.id)).created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        elif case == 'revoked':
            principal = await db.scalar(select(ExternalPrincipal).where(ExternalPrincipal.tenant_id == seed['tenant_id']))
            principal.status = 'revoked'
        elif case == 'bot_replaced':
            config = await db.get(TenantChannelConfig, seed['configs']['wecom'].id)
            config.extra_config = {**config.extra_config, 'wecom_bot_id': 'replacement-bot'}
        elif case == 'two_outputs':
            task = await db.get(RuntimeTask, UUID(admitted['runtime_task_id']))
            await enqueue_terminal_delivery_for_task(db, task=task, content='One clarification is needed.', terminal_status='clarification_required',
                delivery_kind='interactive_prompt', prompt_item_id=uuid4())
        await db.commit()
    factory = httpx.AsyncClient
    def provider(request):
        calls.append(request)
        assert request.url.path == '/cgi-bin/aibot/response'
        if case == 'unknown':
            raise httpx.ReadTimeout('synthetic-private-response', request=request)
        return httpx.Response(200, json={'errcode': 0})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(**{**kwargs, 'transport': httpx.MockTransport(provider)}))
    service = ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker)
    if case == 'two_outputs':
        import asyncio
        await asyncio.gather(service.drain_once(worker_id='bot-one', limit=1), service.drain_once(worker_id='bot-two', limit=1))
    else:
        await service.drain_once(worker_id='bot-return', limit=20)
    before = len(calls)
    await service.drain_once(worker_id='bot-recovery', limit=20)
    assert len(calls) == before == (1 if case in {'unknown', 'two_outputs'} else 0)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        rows = list(await db.scalars(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == UUID(admitted['runtime_task_id']))))
        if case == 'two_outputs':
            assert len(rows) == 2 and sum(row.status == 'delivered' for row in rows) == 1
            assert all(row.status in {'delivered', 'dead_letter', 'needs_reconciliation'} for row in rows)
        else:
            row, = rows
            assert row.id == delivery
            assert row.status == ('needs_reconciliation' if case == 'unknown' else 'dead_letter')
        event = await db.get(ChannelIngressEvent, item.id)
        claim = (event.metadata_json or {}).get('smartbot_response_claim') or {}
        assert claim.get('state') == ('unknown' if case == 'unknown' else 'delivered' if case == 'two_outputs' else None)
