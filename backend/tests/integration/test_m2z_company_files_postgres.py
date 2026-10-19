"""Company applications return native Office artifacts to the original verified work."""

import hashlib
import json
from uuid import UUID
from zipfile import ZipFile

import httpx
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.runtime_task import RuntimeTask
from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
from app.services.channel_ingress_inbox import ChannelIngressInboxService
from app.services.runtime_task_claim_service import RuntimeTaskClaimService
from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
from app.services.web_chat_runtime import execute_web_chat_run
from tests.integration import test_m2y_company_channel_identity_postgres as _company
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

company_messaging = _company.company_messaging
company_runtime = _company.company_runtime


def _call(name, arguments):
    return {'tool_calls': [{'id': name, 'type': 'function', 'function': {'name': name,
        'arguments': json.dumps(arguments)}}], 'finish_reason': 'tool_calls'}


async def _verified_wecom_work(seed, api, text, message_id):
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    issued = await api.post('/tenant-channels/wecom/identity/challenge', json=scope)
    assert issued.status_code == 200, issued.text
    config = seed['configs']['wecom']
    url = f"/channel/wecom/tenant/{seed['tenant_id']}/webhook"
    for content, event_id in [(issued.json()['command'], 'verify-office'), (text, message_id)]:
        body, params = _company.company_wecom_request(config, text=content, message_id=event_id)
        accepted = await api.post(url, content=body, params=params)
        assert accepted.status_code == 200, accepted.text


@pytest.mark.parametrize('file_receipt', ['delivered', 'unknown', 'invalid_recipient'])
async def test_company_wecom_returns_actual_native_spreadsheet_via_original_outbox(
    company_runtime, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, file_receipt,
):
    seed = company_runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        await _verified_wecom_work(seed, api, 'Create a spreadsheet with the amount 17 in cell A1.', 'office-work')
    inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    item = next(row for row in await inbox.claim_batch(worker_id='office-company', limit=20)
        if row.provider_event_id == 'office-work' and row.tenant_id == seed['tenant_id'])
    admitted = await dispatch_channel_ingress_event(item)
    script = ScriptedLLMClient([
        _call('tool_search', {'query': 'office_document_create office_document_apply'}),
        _call('office_document_create', {'path': 'workspace/company-report.xlsx', 'kind': 'xlsx'}),
        _call('office_document_apply', {'path': 'workspace/company-report.xlsx', 'operations': [
            {'command': 'set', 'path': '/Sheet1/A1', 'props': {'value': '17'}}]}),
        {'content': 'The [company spreadsheet](workspace/company-report.xlsx) is ready.', 'finish_reason': 'stop'},
    ])
    monkeypatch.setattr('app.runtime.invoker.create_llm_client', lambda **_kwargs: script)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        claims = await RuntimeTaskClaimService(db=db, worker_id='office-company-worker', lease_seconds=600).claim_available()
        claim = next(row for row in claims if row.id == UUID(admitted['runtime_task_id']))
        await db.commit()
    fence = set_runtime_task_fence(task_id=claim.id, worker_id='office-company-worker', claim_version=claim.claim_version)
    try:
        await execute_web_chat_run(claim.id)
    finally:
        reset_runtime_task_fence(fence)
    assert len(script.requests) == 4
    assert any(tool['function']['name'] == 'office_document_create' for tool in script.requests[1]['tools'])
    path = seed['agent_root'] / 'workspace/company-report.xlsx'
    with ZipFile(path) as document:
        from xml.etree import ElementTree
        sheet = ElementTree.fromstring(document.read('xl/worksheets/sheet1.xml'))
        assert sheet.find('.//{*}c[@r="A1"]/{*}v').text == '17'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        task = await db.get(RuntimeTask, claim.id)
        assert task.status == 'completed' and task.root_user_id == seed['user_id'], (task.status, task.result_summary)
        outbox = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == task.id))
        assert outbox is not None and outbox.artifact_ids_json
        assert outbox.channel_config_id == seed['configs']['wecom'].id
        assert outbox.delivery_target_json['user_id'] == 'member-one'
        from app.models.invocation_span import InvocationSpan
        proofs = list(await db.scalars(select(InvocationSpan).where(InvocationSpan.runtime_task_id == task.id,
            InvocationSpan.span_type == 'workspace_output')))
        assert len(proofs) == 2
        for span in proofs:
            metadata = span.metadata_json
            assert metadata['native_content_receipts']['status'] == 'captured', metadata
            proof = metadata['workspace_execution_evidence']
            writer, = proof['descriptor_writes']
            assert writer['verified'] and writer['descriptor']
            assert writer['observed_output_sha256'] == writer['expected_output_sha256']
            assert proof['execution_inputs']['observed'] and not proof['execution_inputs']['unbound']
            if span.name == 'office_document_apply':
                assert proof['execution_inputs']['sources']
                assert writer['observed_output_sha256'] == digest
    calls = []
    factory = httpx.AsyncClient
    def provider(request):
        calls.append(request)
        assert request.url.host == 'qyapi.weixin.qq.com'
        if request.url.path.endswith('/gettoken'):
            return httpx.Response(200, json={'errcode': 0, 'access_token': 'synthetic-company-token'})
        if request.url.path.endswith('/media/upload'):
            return httpx.Response(200, json={'errcode': 0, 'media_id': 'synthetic-office-media', 'type': 'file'})
        payload = json.loads(request.content)
        assert payload['touser'] == 'member-one' and payload['agentid'] == 100001
        if payload['msgtype'] == 'file':
            assert payload['file']['media_id'] == 'synthetic-office-media'
            if file_receipt == 'unknown':
                raise httpx.ReadTimeout('synthetic lost response', request=request)
            if file_receipt == 'invalid_recipient':
                return httpx.Response(200, json={'errcode': 0, 'invaliduser': 'member-one'})
        return httpx.Response(200, json={'errcode': 0, 'msgid': 'synthetic-company-delivery'})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(**{**kwargs, 'transport': httpx.MockTransport(provider)}))
    await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='office-return', limit=20)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        delivered = await db.get(ChannelDeliveryOutbox, outbox.id)
        expected = {'delivered': 'delivered', 'unknown': 'needs_reconciliation', 'invalid_recipient': 'dead_letter'}[file_receipt]
        assert delivered.status == expected, (delivered.status, delivered.delivery_receipts_json)
        assert delivered.delivery_receipts_json['text']['state'] == 'delivered'
        state = {'delivered': 'delivered', 'unknown': 'unknown', 'invalid_recipient': 'failed'}[file_receipt]
        assert all(part['state'] == state for part in delivered.delivery_receipts_json['artifacts'].values())
    before = len(calls)
    await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='office-return-retry', limit=20)
    assert len(calls) == before
    uploads = [request for request in calls if request.url.path.endswith('/media/upload')]
    assert len(uploads) == 1 and b'company-report.xlsx' in uploads[0].content
    from email import policy
    from email.parser import BytesParser
    mime = BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: ' + uploads[0].headers['content-type'].encode() + b'\r\n\r\n' + uploads[0].content)
    transferred = next(part.get_payload(decode=True) for part in mime.iter_parts() if part.get_param('name', header='content-disposition') == 'media')
    assert hashlib.sha256(transferred).hexdigest() == digest
    (tmp_path / 'office-return-observation.json').write_text(json.dumps({'session_id': admitted['session_id'],
        'runtime_task_id': str(claim.id), 'sha256': digest, 'provider_receipts': delivered.delivery_receipts_json}, indent=2))
