"""Authenticated company files become immutable inputs in the sender's original work."""

import hashlib
import json
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text

from app.api.tenant_channels import _config_revision
from app.database import pin_rls_tenant_context
from app.models.session_v2 import SessionTurnInput
from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
from app.services.channel_ingress_inbox import ChannelIngressInboxService, channel_installation_ref
from app.services.company_dingtalk_stream import accept_company_dingtalk_message
from tests.integration import test_m2y_company_channel_identity_postgres as _company

company_messaging = _company.company_messaging
company_runtime = _company.company_runtime


async def _run_native_work(seed, db_factory, monkeypatch, admitted, script, worker):
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    monkeypatch.setattr('app.runtime.invoker.create_llm_client', lambda **_kwargs: script)
    async with db_factory() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        claim = next(row for row in await RuntimeTaskClaimService(db=db, worker_id=worker, lease_seconds=600).claim_available()
            if row.id == UUID(admitted['runtime_task_id']))
        await db.commit()
    fence = set_runtime_task_fence(task_id=claim.id, worker_id=worker, claim_version=claim.claim_version)
    try:
        await execute_web_chat_run(claim.id)
    finally:
        reset_runtime_task_fence(fence)
    return claim


async def test_verified_dingtalk_file_is_encrypted_deduplicated_and_bound_to_original_work(
    company_runtime, app_user_sessionmaker, owner_sessionmaker, monkeypatch,
):
    seed, calls = company_runtime, []
    config = seed['configs']['dingtalk']
    async def inbound(body):
        return await accept_company_dingtalk_message(tenant_id=seed['tenant_id'], config_id=config.id,
            revision=_config_revision(config), installation_ref=channel_installation_ref(config, fallback=''), body=body)
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        issued = await api.post('/tenant-channels/dingtalk/identity/challenge', json=scope)
        assert issued.status_code == 200, issued.text
        await inbound(_company.company_dingtalk_body(config, issued.json()['command'], 'verify-file-member'))
    body = _company.company_dingtalk_body(config, '', 'company-original-file')
    body.pop('text')
    body.update(msgtype='file', content={'downloadCode': 'synthetic-private-download-code', 'fileName': 'amount]report.csv'})
    accepted = await inbound(body)
    assert accepted['routed'], accepted
    assert (await inbound(body))['event_id'] == accepted['event_id']
    async with owner_sessionmaker() as db:
        raw = await db.scalar(text('SELECT payload_json::text FROM channel_ingress_events WHERE id=:id'),
            {'id': UUID(accepted['event_id'])})
        assert 'synthetic-private-download-code' not in raw
    original = b'amount\n17\n'
    factory = httpx.AsyncClient
    def provider(request):
        calls.append(request)
        if request.url.path == '/v1.0/oauth2/accessToken':
            return httpx.Response(200, json={'accessToken': 'synthetic-v1-token', 'expireIn': 7200})
        if request.url.path == '/v1.0/robot/messageFiles/download':
            assert json.loads(request.content) == {'robotCode': config.app_id, 'downloadCode': 'synthetic-private-download-code'}
            return httpx.Response(200, json={'downloadUrl': 'https://download.dingtalk.com/company-source.csv?token=synthetic-cdn-token'})
        if request.url.path == '/gettoken':
            return httpx.Response(200, json={'errcode': 0, 'access_token': 'synthetic-oapi-token'})
        if request.url.path == '/media/upload':
            assert request.url.params['access_token'] == 'synthetic-oapi-token'
            return httpx.Response(200, json={'errcode': 0, 'media_id': '@synthetic-file-media'})
        if request.url.path == '/v1.0/robot/oToMessages/batchSend':
            payload = json.loads(request.content)
            assert payload['robotCode'] == config.app_id and payload['userIds'] == ['member-one']
            assert payload['msgKey'] == 'sampleFile'
            assert json.loads(payload['msgParam']) == {'mediaId': '@synthetic-file-media',
                'fileName': 'attachment-report.xlsx', 'fileType': 'xlsx'}
            return httpx.Response(200, json={'processQueryKey': 'synthetic-provider-receipt'})
        if request.url.path == '/v1.0/robot/oToMessages/readStatus':
            assert request.url.params['processQueryKey'] == 'synthetic-provider-receipt'
            return httpx.Response(200, json={'sendStatus': 'SUCESS', 'messageReadInfoList': []})
        if request.url.path == '/robot/sendBySession':
            return httpx.Response(200, json={'errcode': 0})
        assert request.url.host == 'download.dingtalk.com'
        assert 'authorization' not in request.headers and 'x-acs-dingtalk-access-token' not in request.headers
        return httpx.Response(200, content=original, headers={'Content-Type': 'text/csv'})
    async def public(hostname, port):
        assert hostname == 'download.dingtalk.com' and port == 443
        return ('93.184.216.34',)
    monkeypatch.setattr('app.services.governed_egress.resolve_public_addresses', public)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(**{**kwargs, 'transport': httpx.MockTransport(provider)}))
    inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    items = [row for row in await inbox.claim_batch(worker_id='company-files', limit=20)
        if row.provider_event_id == 'company-original-file']
    assert len(items) == 1
    admitted = await dispatch_channel_ingress_event(items[0])
    recovered = await dispatch_channel_ingress_event(items[0])
    assert UUID(recovered['runtime_task_id']) == UUID(admitted['runtime_task_id'])
    assert UUID(recovered['session_id']) == UUID(admitted['session_id'])
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        turn_input = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.id == UUID(admitted['input_id'])))
        attachment, = [part['attachment'] for part in turn_input.content_parts_json if part['type'] == 'attachment_ref']
        path = attachment['path']
        assert path.startswith('workspace/uploads/dingtalk/') and path.endswith('/amount]report.csv')
        assert (seed['agent_root'] / path).read_bytes() == original
        assert 'synthetic-private-download-code' not in json.dumps(turn_input.content_parts_json)
        from app.services.content_input_receipts import latest_workspace_receipt
        receipt = await latest_workspace_receipt(db, seed['tenant_id'], seed['agent_id'], path)
        assert receipt.content_sha256 == hashlib.sha256(original).hexdigest()
        assert turn_input.session_id == UUID(admitted['session_id'])
    assert len(calls) == 3
    from app.api import files
    from app.database import get_db
    from app.core.security import create_access_token
    from app.models.user import User
    seed['app'].include_router(files.router)
    async with owner_sessionmaker() as db:
        user = await db.get(User, seed['user_id'])
        jwt_token = create_access_token(str(user.id), user.role, str(user.tenant_id))
    async def scoped_file_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            yield db
            await db.commit()
    seed['app'].dependency_overrides[get_db] = scoped_file_db
    async with factory(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company',
        headers={'Authorization': 'Bearer ' + jwt_token}) as web:
        downloaded = await web.get(f"/agents/{seed['agent_id']}/files/download", params={'path': path})
        assert downloaded.status_code == 200 and downloaded.content == original, downloaded.text
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from tests.integration.test_m2z_company_files_postgres import _call
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient
    script = ScriptedLLMClient([
        _call('tool_search', {'query': 'read_file office_document_create office_document_apply'}),
        _call('read_file', {'path': path}),
        _call('office_document_create', {'path': 'workspace/attachment-report.xlsx', 'kind': 'xlsx'}),
        _call('office_document_apply', {'path': 'workspace/attachment-report.xlsx', 'operations': [
            {'command': 'set', 'path': '/Sheet1/A1', 'props': {'value': '17'}}]}),
        {'content': 'The [Office result](workspace/attachment-report.xlsx) is ready.', 'finish_reason': 'stop'},
    ])
    claim = await _run_native_work(seed, app_user_sessionmaker, monkeypatch, admitted, script, 'attachment-worker')
    assert len(script.requests) == 5
    assert any(message.get('role') == 'tool' and 'amount' in str(message.get('content')) and '17' in str(message.get('content'))
        for message in script.requests[2]['messages'])
    assert all('synthetic-private-download-code' not in json.dumps(request) and 'synthetic-cdn-token' not in json.dumps(request)
        for request in script.requests)
    await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='attachment-return', limit=20)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        outbox = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == claim.id))
        assert outbox.status == 'delivered', (outbox.status, outbox.delivery_receipts_json)
        assert len(outbox.artifact_ids_json) == 1
        from app.models.invocation_span import InvocationSpan
        from app.models.session_v2 import SessionModelResult
        proof = await db.scalar(select(InvocationSpan).where(InvocationSpan.runtime_task_id == claim.id,
            InvocationSpan.span_type == 'workspace_input', InvocationSpan.name == 'read_file'))
        assert proof.metadata_json['workspace_input_evidence']['observed_sha256'] == hashlib.sha256(original).hexdigest()
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == claim.id)))
        assert len(models) == 5
        assert all(model.model_request_snapshot_json['input_bindings']['dependency_coverage']['complete'] for model in models)
        assert all(part['detail']['provider_reference'] == 'synthetic-provider-receipt'
            for part in outbox.delivery_receipts_json['artifacts'].values())
    uploads = [r for r in calls if r.url.path == '/media/upload']
    assert len(uploads) == 1 and b'attachment-report.xlsx' in uploads[0].content
    from email import policy
    from email.parser import BytesParser
    mime = BytesParser(policy=policy.default).parsebytes(b'Content-Type: ' + uploads[0].headers['content-type'].encode() + b'\r\n\r\n' + uploads[0].content)
    transferred = next(p.get_payload(decode=True) for p in mime.iter_parts() if p.get_param('name', header='content-disposition') == 'media')
    assert transferred == (seed['agent_root'] / 'workspace/attachment-report.xlsx').read_bytes()
    import io
    from zipfile import ZipFile
    from xml.etree import ElementTree
    with ZipFile(io.BytesIO(transferred)) as document:
        sheet = ElementTree.fromstring(document.read('xl/worksheets/sheet1.xml'))
        assert sheet.find('.//{*}c[@r="A1"]/{*}v').text == '17'


@pytest.mark.parametrize('kind', ['image', 'voice', 'video'])
async def test_signed_wecom_supported_media_enters_the_same_receipted_work(
    company_runtime, app_user_sessionmaker, owner_sessionmaker, monkeypatch, kind,
):
    seed, calls = company_runtime, []
    config = seed['configs']['wecom']
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        issued = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
        assert issued.status_code == 200, issued.text
        for value, event_id, message_type in [(issued.json()['command'], 'verify-media-member', 'text'), ('', f'company-{kind}', kind)]:
            body, params = _company.company_wecom_request(config, text=value, message_id=event_id, msg_type=message_type,
                media_id='synthetic-private-media-id' if message_type != 'text' else None)
            response = await api.post(f"/channel/wecom/tenant/{seed['tenant_id']}/webhook", content=body, params=params)
            assert response.status_code == 200, response.text
    inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    item = next((row for row in await inbox.claim_batch(worker_id='company-media', limit=20)
        if row.provider_event_id == f'company-{kind}'), None)
    assert item is not None, 'Signed WeCom media must enter durable company ingress'
    async with owner_sessionmaker() as db:
        raw = await db.scalar(text('SELECT payload_json::text FROM channel_ingress_events WHERE id=:id'), {'id': item.id})
        assert 'synthetic-private-media-id' not in raw
    factory, media = httpx.AsyncClient, b'synthetic-provider-media-bytes'
    def provider(request):
        calls.append(request)
        assert request.url.host == 'qyapi.weixin.qq.com'
        if request.url.path.endswith('/gettoken'):
            return httpx.Response(200, json={'errcode': 0, 'access_token': 'synthetic-wecom-token'})
        assert request.url.path.endswith('/media/get') and request.url.params['media_id'] == 'synthetic-private-media-id'
        assert 'authorization' not in request.headers
        return httpx.Response(200, content=media, headers={'Content-Type': {'image': 'image/png', 'voice': 'audio/amr', 'video': 'video/mp4'}[kind]})
    async def public(hostname, port):
        assert hostname == 'qyapi.weixin.qq.com' and port == 443
        return ('93.184.216.34',)
    monkeypatch.setattr('app.services.governed_egress.resolve_public_addresses', public)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(**{**kwargs, 'transport': httpx.MockTransport(provider)}))
    admitted = await dispatch_channel_ingress_event(item)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        row = await db.get(SessionTurnInput, UUID(admitted['input_id']))
        attachment, = [part['attachment'] for part in row.content_parts_json if part['type'] == 'attachment_ref']
        assert attachment['path'].startswith('workspace/uploads/wecom/')
        assert (seed['agent_root'] / attachment['path']).read_bytes() == media
        from app.services.content_input_receipts import latest_workspace_receipt
        receipt = await latest_workspace_receipt(db, seed['tenant_id'], seed['agent_id'], attachment['path'])
        assert receipt.content_sha256 == hashlib.sha256(media).hexdigest()
        assert 'synthetic-private-media-id' not in json.dumps(row.content_parts_json)
    assert len(calls) == 2


async def test_disconnected_company_attachment_never_fetches_provider_bytes(
    company_runtime, app_user_sessionmaker, monkeypatch,
):
    seed = company_runtime
    config = seed['configs']['dingtalk']
    async def inbound(body):
        return await accept_company_dingtalk_message(tenant_id=seed['tenant_id'], config_id=config.id,
            revision=_config_revision(config), installation_ref=channel_installation_ref(config, fallback=''), body=body)
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        issued = await api.post('/tenant-channels/dingtalk/identity/challenge', json=scope)
        assert issued.status_code == 200, issued.text
        await inbound(_company.company_dingtalk_body(config, issued.json()['command'], 'verify-revoke-file'))
        body = _company.company_dingtalk_body(config, '', 'revoked-company-file')
        body.pop('text')
        body.update(msgtype='file', content={'downloadCode': 'synthetic-download-code', 'fileName': 'private.csv'})
        assert (await inbound(body))['routed']
        identity = (await api.get('/tenant-channels/dingtalk/identity', params=scope)).json()
        response = await api.post('/tenant-channels/dingtalk/identity/disconnect', json={**scope, 'principal_id': identity['principal_id']})
        assert response.status_code == 200, response.text
    def forbidden(**_kwargs):
        pytest.fail('A disconnected principal cannot create a provider HTTP client')
    monkeypatch.setattr(httpx, 'AsyncClient', forbidden)
    inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    item = next(row for row in await inbox.claim_batch(worker_id='revoked-attachment', limit=20)
        if row.provider_event_id == 'revoked-company-file')
    with pytest.raises(PermissionError, match='company_channel_original_employee_unavailable'):
        await dispatch_channel_ingress_event(item)
    assert not list((seed['agent_root'] / 'workspace/uploads').rglob('*'))


async def test_unavailable_company_file_keeps_a_safe_reply_in_original_work(
    company_runtime, app_user_sessionmaker, monkeypatch,
):
    seed = company_runtime
    config = seed['configs']['dingtalk']
    async def inbound(body):
        return await accept_company_dingtalk_message(tenant_id=seed['tenant_id'], config_id=config.id,
            revision=_config_revision(config), installation_ref=channel_installation_ref(config, fallback=''), body=body)
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        issued = await api.post('/tenant-channels/dingtalk/identity/challenge', json=scope)
        assert issued.status_code == 200, issued.text
        await inbound(_company.company_dingtalk_body(config, issued.json()['command'], 'verify-expired-file'))
        body = _company.company_dingtalk_body(config, '', 'expired-company-file')
        body.pop('text')
        body.update(msgtype='file', content={'downloadCode': 'synthetic-expired-code', 'fileName': 'source.csv'})
        assert (await inbound(body))['routed']
    factory = httpx.AsyncClient
    def provider(request):
        if request.url.path == '/v1.0/oauth2/accessToken':
            return httpx.Response(200, json={'accessToken': 'synthetic-v1-token'})
        if request.url.path == '/robot/sendBySession':
            return httpx.Response(200, json={'errcode': 0})
        assert request.url.path == '/v1.0/robot/messageFiles/download'
        return httpx.Response(403, json={'code': 'synthetic-private-provider-diagnostic'})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(**{**kwargs, 'transport': httpx.MockTransport(provider)}))
    inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    item = next(row for row in await inbox.claim_batch(worker_id='unavailable-file', limit=20)
        if row.provider_event_id == 'expired-company-file')
    admitted = await dispatch_channel_ingress_event(item)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        row = await db.get(SessionTurnInput, UUID(admitted['input_id']))
        reference, = row.content_parts_json
        assert reference['attachment'] == {'name': 'source.csv', 'state': 'unavailable',
            'reason_code': 'company_file_provider_unavailable', 'recovery_action': 'upload_new_message'}
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient
    script = ScriptedLLMClient([{'content': 'Please upload the file again in this private conversation.', 'finish_reason': 'stop'}])
    claim = await _run_native_work(seed, app_user_sessionmaker, monkeypatch, admitted, script, 'unavailable-file-worker')
    assert 'unavailable' in json.dumps(script.requests[0]['messages'])
    assert 'synthetic-expired-code' not in json.dumps(script.requests)
    await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='unavailable-file-reply', limit=20)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        outbox = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == claim.id))
        assert outbox.status == 'delivered' and not outbox.artifact_ids_json
    assert not list((seed['agent_root'] / 'workspace/uploads').rglob('*'))
