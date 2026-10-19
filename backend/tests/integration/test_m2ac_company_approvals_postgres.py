"""Company channel approvals remain enterprise tickets in their original work."""

from uuid import UUID
import json

import httpx
import pytest
from sqlalchemy import select

from app.api.tenant_channels import _config_revision
from app.database import pin_rls_tenant_context
from app.services.channel_ingress_inbox import ChannelIngressInboxService, channel_installation_ref
from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
from app.services.company_dingtalk_stream import accept_company_dingtalk_message
from tests.integration import test_m2aa_company_attachments_postgres as _files
from tests.integration import test_m2ab_company_smartbot_postgres as _bot
from tests.integration import test_m2y_company_channel_identity_postgres as _company

company_messaging = _company.company_messaging
company_runtime = _company.company_runtime


async def _assert_approved_output_proof(seed, db, ticket):
    from copy import deepcopy
    from types import SimpleNamespace
    from uuid import uuid4, uuid5
    from app.models.audit import ApprovalRequest
    from app.models.invocation_span import InvocationSpan
    from app.models.organization_identity import ContentInputReceipt
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.models.chat_session import ChatSession
    from app.services.content_input_receipts import load_approved_workspace_result, persist_workspace_execution_evidence
    from app.services.session_approval import group_approval_origin
    from app.tools.runtime import ToolExecutionContext

    session = await db.get(ChatSession, UUID(ticket.execution_envelope['session_id']))
    original, invocation, _event = await group_approval_origin(db, session=session, approval=ticket)
    invocation_id, original_id, ticket_id, session_id = invocation.id, original.id, ticket.id, session.id
    call_id, arguments = invocation.provider_tool_use_id, dict(ticket.normalized_arguments)
    positive = await load_approved_workspace_result(db, approval=ticket)
    assert positive is not None and len(positive[1]) == 1
    receipt_id, = positive[1]
    span_id = uuid5(invocation_id, 'workspace-output-evidence')
    for mutation in ('job', 'descriptor', 'model', 'receipt', 'arguments', 'capture'):
        savepoint = await db.begin_nested()
        ticket = await db.get(ApprovalRequest, ticket_id)
        span = await db.get(InvocationSpan, span_id)
        if mutation == 'job':
            (await db.get(RuntimeTask, ticket.execution_task_id)).status = 'pending'
        elif mutation == 'descriptor':
            data = deepcopy(span.metadata_json)
            data['workspace_execution_evidence']['descriptor_writes'][0]['verified'] = False
            span.metadata_json = data
        elif mutation == 'model':
            model = await db.get(SessionModelResult, UUID(span.metadata_json['workspace_execution_evidence']['model_result_id']))
            model.model_request_hash = '0' * 64
        elif mutation == 'receipt':
            (await db.get(ContentInputReceipt, receipt_id)).content_sha256 = '0' * 64
        elif mutation == 'arguments':
            ticket.normalized_arguments = {**arguments, 'path': 'workspace/other.xlsx'}
        else:
            data = deepcopy(ticket.execution_receipt)
            data['runtime_evidence']['workspace_execution_evidence']['receipt_ref'] = 'workspace-output:' + str(uuid4())
            ticket.execution_receipt = data
        await db.flush()
        assert await load_approved_workspace_result(db, approval=ticket) is None, mutation
        await savepoint.rollback()
        db.expire_all()
    ticket = await db.get(ApprovalRequest, ticket_id)
    session = await db.get(ChatSession, session_id)
    assert await load_approved_workspace_result(db, approval=ticket) == positive
    context = ToolExecutionContext(agent_id=seed['agent_id'], user_id=seed['user_id'], tenant_id=str(seed['tenant_id']),
        workspace=seed['agent_root'], session_id=str(session.id), runtime_task_id=str(original_id))
    denied = await persist_workspace_execution_evidence(context=context, tool_name=ticket.tool_name,
        tool_call_id=call_id, arguments=arguments, result=ticket.execution_result, states={}, lineage=[], errors={})
    assert denied == {'status': 'unavailable', 'reason_code': 'workspace_execution_scope_mismatch'}

    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_result import RuntimeResultObject
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.runtime_result_sources import approved_workspace_connector_sources, validate_runtime_result_sources
    row = await db.get(RuntimeNotificationOutbox, UUID(ticket.execution_receipt['continuation_outbox_id']))
    result = await db.get(RuntimeResultObject, row.result_object_id)
    assert await approved_workspace_connector_sources(db, result=result, agent_id=seed['agent_id'], user_id=uuid4()) == []
    assert await approved_workspace_connector_sources(db, result=result, agent_id=uuid4(), user_id=seed['user_id']) == []
    for reader, work in ((SimpleNamespace(id=uuid4()), session),
        (SimpleNamespace(id=seed['user_id']), SimpleNamespace(agent_id=seed['agent_id'], id=uuid4()))):
        with pytest.raises(ContentInputReceiptError) as refused:
            await validate_runtime_result_sources(db, None, result, user=reader, session=work)
        assert refused.value.status_code == 403


async def _admit(seed, owner_factory, provider):
    smartbot = provider == 'smart_bot'
    channel = 'wecom' if smartbot else provider
    config = await _bot._configure(seed, owner_factory, 'plaintext') if smartbot else seed['configs'][channel]
    scope = {'expected_tenant_id': str(seed['tenant_id']), 'expected_user_id': str(seed['user_id'])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company') as api:
        code = await api.post(f'/tenant-channels/{channel}/identity/challenge', json=scope)
        assert code.status_code == 200, code.text
        for content, event in [(code.json()['command'], 'approve-member'), ('Create the requested workbook.', 'approve-work')]:
            if channel == 'dingtalk':
                await accept_company_dingtalk_message(tenant_id=seed['tenant_id'], config_id=config.id,
                    revision=_config_revision(config), installation_ref=channel_installation_ref(config, fallback=''),
                    body=_company.company_dingtalk_body(config, content, event))
            else:
                if smartbot:
                    body, params = _bot._request(config, content=content, subject='member-one', event=event)
                    response = await api.post(f"/channel/wecom/tenant/{seed['tenant_id']}/webhook", json=body, params=params)
                else:
                    body, params = _company.company_wecom_request(config, text=content, message_id=event)
                    response = await api.post(f"/channel/wecom/tenant/{seed['tenant_id']}/webhook", content=body, params=params)
                assert response.status_code == 200, response.text


@pytest.mark.parametrize('provider', ['dingtalk', 'wecom', 'smart_bot'])
async def test_company_office_approval_is_not_silent_or_a_local_permission(
    company_runtime, app_user_sessionmaker, owner_sessionmaker, monkeypatch, provider,
):
    from app.models.audit import ApprovalRequest
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.guard_policy import GuardPolicy
    from app.models.runtime_task import RuntimeTask
    from app.config import get_settings
    from tests.integration.test_m2z_company_files_postgres import _call
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed = company_runtime
    monkeypatch.setattr(get_settings(), 'PUBLIC_BASE_URL', 'https://work.fixture.test')
    async with owner_sessionmaker() as db:
        db.add(GuardPolicy(tenant_id=seed['tenant_id'], zone_guard={'tool_rules': [
            {'tools': ['office_document_create'], 'decision': 'require_approval'}]}))
        await db.commit()
    await _admit(seed, owner_sessionmaker, provider)
    inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    item, = [row for row in await inbox.claim_batch(worker_id='company-approval', limit=20) if row.provider_event_id == 'approve-work']
    admitted = await dispatch_channel_ingress_event(item)
    script = ScriptedLLMClient([
        _call('tool_search', {'query': 'office_document_create'}),
        _call('office_document_create', {'path': 'workspace/approved-report.xlsx', 'kind': 'xlsx'}),
    ])
    claim = await _files._run_native_work(seed, app_user_sessionmaker, monkeypatch, admitted, script, 'company-approval-worker')
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        task = await db.get(RuntimeTask, claim.id)
        assert task.status == 'completed' and task.result_summary == 'awaiting_company_approval'
        ticket = await db.scalar(select(ApprovalRequest).where(ApprovalRequest.tenant_id == seed['tenant_id'],
            ApprovalRequest.agent_id == seed['agent_id'], ApprovalRequest.tool_name == 'office_document_create'))
        assert ticket is not None and ticket.status == 'pending' and ticket.requested_by == seed['user_id']
        assert ticket.requested_by_external_principal_id is not None and ticket.consumed_at is None
        assert UUID(ticket.execution_envelope['session_id']) == UUID(admitted['session_id'])
        assert not (seed['agent_root'] / 'workspace/approved-report.xlsx').exists()
        notice = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == task.id,
            ChannelDeliveryOutbox.delivery_kind == 'interactive_prompt'))
        assert notice is not None, 'A company approval pause must return a durable notice to the original channel work'
        assert notice.session_id == UUID(admitted['session_id']) and notice.user_id == seed['user_id']
        assert str(ticket.id) in notice.text_content
        assert f"/agents/{seed['agent_id']}/sessions/{admitted['session_id']}" in notice.text_content
        approval_id, notice_id = ticket.id, notice.id
    calls, factory = [], httpx.AsyncClient
    def transport(request):
        calls.append(request)
        if request.url.path.endswith('/gettoken'):
            return httpx.Response(200, json={'errcode': 0, 'access_token': 'synthetic-company-token'})
        if request.url.path.endswith('/media/upload'):
            return httpx.Response(200, json={'errcode': 0, 'media_id': '@synthetic-approved-office'})
        if request.url.path.endswith('/message/send'):
            assert json.loads(request.content)['touser'] == 'member-one'
            return httpx.Response(200, json={'errcode': 0, 'msgid': 'synthetic-approved-return'})
        if request.url.path.endswith('/robot/sendBySession') or request.url.path.endswith('/aibot/response'):
            return httpx.Response(200, json={'errcode': 0})
        if request.url.path == '/v1.0/oauth2/accessToken':
            return httpx.Response(200, json={'accessToken': 'synthetic-v1-token'})
        if request.url.path.endswith('/oToMessages/batchSend'):
            assert json.loads(request.content)['userIds'] == ['member-one']
            return httpx.Response(200, json={'processQueryKey': 'synthetic-approved-office'})
        assert request.url.path.endswith('/oToMessages/readStatus')
        return httpx.Response(200, json={'sendStatus': 'SUCESS'})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(**{**kwargs, 'transport': httpx.MockTransport(transport)}))
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    delivery = ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker)
    await delivery.drain_once(worker_id='pending-approval-return', limit=20)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        assert (await db.get(ChannelDeliveryOutbox, notice_id)).status == 'delivered'
    from app.api import chat_sessions, files
    from app.core.security import create_access_token, get_current_user
    from app.database import get_db
    from app.models.user import User
    seed['app'].include_router(chat_sessions.router)
    seed['app'].include_router(files.router)
    seed['app'].dependency_overrides.pop(get_current_user)
    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            yield db
            await db.commit()
    seed['app'].dependency_overrides[get_db] = scoped_db
    async with owner_sessionmaker() as db:
        user = await db.get(User, seed['user_id'])
        jwt = create_access_token(str(user.id), user.role, str(user.tenant_id))
    path = f"/agents/{seed['agent_id']}/sessions/{admitted['session_id']}/approvals/{approval_id}"
    async with factory(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company', headers={'Authorization': 'Bearer ' + jwt}) as web:
        current = await web.get(path)
        assert current.status_code == 200 and current.json()['can_resolve'], current.text
        wrong = path.replace(admitted['session_id'], str(seed['session_id']))
        assert (await web.post(wrong + '/resolve', json={'action': 'approve'})).status_code == 404
        decided = await web.post(path + '/resolve', json={'action': 'approve'})
        assert decided.status_code == 200 and decided.json()['status'] == 'approved', decided.text
        assert (await web.post(path + '/resolve', json={'action': 'approve'})).status_code == 200
        assert (await web.post(path + '/resolve', json={'action': 'reject'})).status_code == 409
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        ticket = await db.get(ApprovalRequest, approval_id)
        job_id = ticket.execution_task_id
        jobs = await RuntimeTaskClaimService(db=db, worker_id='company-approved-effect', task_types=('approval_execution',), lease_seconds=600).claim_available()
        assert len(jobs) == 1 and jobs[0].id == job_id
        await db.commit()
    assert await run_claimed_runtime_task(execute_claimed_approval_execution(job_id), task_id=job_id,
        claim_version=jobs[0].claim_version, worker_id='company-approved-effect', lease_seconds=600) == 'succeeded'
    assert await execute_claimed_approval_execution(job_id) == 'succeeded'
    assert (seed['agent_root'] / 'workspace/approved-report.xlsx').is_file()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        ticket = await db.get(ApprovalRequest, approval_id)
        job = await db.get(RuntimeTask, job_id)
        assert ticket.consumed_at and ticket.resolved_by == seed['user_id'] and ticket.execution_status == 'succeeded'
        assert job.root_user_id == seed['user_id'] and UUID(job.parent_session_id) == UUID(admitted['session_id'])
        if provider == 'dingtalk':
            await _assert_approved_output_proof(seed, db, ticket)
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor
    async def summary(_messages, **_kwargs):
        return 'The original company work is waiting for its approved Office action.'
    processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed['data_root'], summary_generator=summary)
    await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=seed['tenant_id'], worker_id='company-source-boundary', canonical_validator=processor.validate,
        process_callback=processor, task_types=('web_chat_turn',))
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    notifications = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    claimed = await notifications.claim_batch(worker_id='company-approved-return', limit=100)
    pages = await notifications.prepare_integration_pages(worker_id='company-approved-return',
        claimed=[row for row in claimed if row.tenant_id == seed['tenant_id']])
    assert len(pages) == 1
    returned = await notifications._deliver_page(pages[0])
    assert returned.get('ok') is True, returned
    assert await notifications._mark_page_delivered(page=pages[0], worker_id='company-approved-return', receipt=returned) == 1
    result_ref = pages[0].manifest['items'][0]['result_ref']
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        successor = await db.scalar(select(RuntimeTask).where(RuntimeTask.tenant_id == seed['tenant_id'],
            RuntimeTask.metadata_json['integration_page_id'].astext == str(pages[0].id)))
        assert successor is not None and successor.root_user_id == seed['user_id']
        assert UUID(successor.parent_session_id) == UUID(admitted['session_id'])
        successor_id = successor.id
    continued = ScriptedLLMClient([
        _call('read_runtime_result', {'result_ref': result_ref}),
        _call('tool_search', {'query': 'office_document_apply'}),
        _call('office_document_apply', {'path': 'workspace/approved-report.xlsx', 'operations': [
            {'command': 'set', 'path': '/Sheet1/A1', 'props': {'value': '17'}}]}),
        {'content': 'The [approved Office result](workspace/approved-report.xlsx) is ready.', 'finish_reason': 'stop'},
    ])
    await _files._run_native_work(seed, app_user_sessionmaker, monkeypatch,
        {'runtime_task_id': str(successor_id)}, continued, 'company-approved-consumer')
    assert len(continued.requests) == 4
    assert 'approved-report.xlsx' in json.dumps(continued.requests[1]['messages'])
    from app.models.chat_artifact import ChatArtifact
    from app.models.session_v2 import SessionModelResult
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == successor_id,
            ChatArtifact.path == 'workspace/approved-report.xlsx'))
        assert artifact is not None and artifact.snapshot_json['content_input_receipt_id']
        assert artifact.session_id == UUID(admitted['session_id'])
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == successor_id)))
        assert len(models) == 4 and all(row.model_request_snapshot_json['input_bindings']['dependency_coverage']['complete'] for row in models)
    await delivery.drain_once(worker_id='company-approved-result', limit=20)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        final = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == successor_id))
        assert final.session_id == UUID(admitted['session_id']) and final.artifact_ids_json == [str(artifact.id)]
        # HTTP Smart Bot already used its sole reply for the pending notice.
        assert final.status == ('dead_letter' if provider == 'smart_bot' else 'delivered'), (final.status, final.delivery_receipts_json)
        if provider == 'smart_bot':
            assert final.delivery_receipts_json['text']['provider_status'] == 'unavailable'
            assert len([request for request in calls if request.url.path.endswith('/aibot/response')]) == 1
    async with factory(transport=httpx.ASGITransport(app=seed['app']), base_url='http://company', headers={'Authorization': 'Bearer ' + jwt}) as web:
        downloaded = await web.get(f"/agents/{seed['agent_id']}/files/download", params={'path': 'workspace/approved-report.xlsx'})
        assert downloaded.status_code == 200, downloaded.text
    from io import BytesIO
    from xml.etree import ElementTree
    from zipfile import ZipFile
    with ZipFile(BytesIO(downloaded.content)) as archive:
        sheet = ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        assert sheet.find('.//{*}c[@r="A1"]/{*}v').text == '17'
    uploads = [request for request in calls if request.url.path.endswith('/media/upload')]
    if provider != 'smart_bot':
        assert len(uploads) == 1 and b'approved-report.xlsx' in uploads[0].content
        from email import policy
        from email.parser import BytesParser
        mime = BytesParser(policy=policy.default).parsebytes(b'Content-Type: ' + uploads[0].headers['content-type'].encode() + b'\r\n\r\n' + uploads[0].content)
        transferred = next(part.get_payload(decode=True) for part in mime.iter_parts() if part.get_param('name', header='content-disposition') == 'media')
        assert transferred == downloaded.content
    before = len(calls)
    await delivery.drain_once(worker_id='company-approved-no-replay', limit=20)
    assert len(calls) == before


@pytest.mark.parametrize('channel,configured', [('wecom', True), ('dingtalk', True), ('feishu', False), ('feishu', True)])
async def test_approval_cards_use_only_the_configured_feishu_connection(
    company_messaging, owner_sessionmaker, app_user_sessionmaker, monkeypatch, channel, configured,
):
    from uuid import uuid4
    from app.models.agent import Agent
    from app.models.audit import ApprovalRequest
    from app.models.channel_config import ChannelConfig
    from app.models.user import User
    from app.services.approval_service import approval_service

    seed, calls, factory = company_messaging, [], httpx.AsyncClient
    approval_id = uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, seed['user_id'])
        user.feishu_user_id = f'feishu-{user.id.hex}'
        db.add(ChannelConfig(tenant_id=seed['tenant_id'], agent_id=seed['agent_id'], channel_type=channel,
            app_id='synthetic-selected-app', app_secret='synthetic-selected-secret', is_configured=configured))
        db.add(ApprovalRequest(id=approval_id, tenant_id=seed['tenant_id'], agent_id=seed['agent_id'],
            action_type='workspace.file.write', requested_by=user.id, details={'summary': 'A work action needs approval.'}))
        await db.commit()
    def provider(request):
        calls.append(request)
        assert request.url.host == 'open.feishu.cn'
        if '/auth/v3/' in request.url.path:
            assert json.loads(request.content) == {'app_id': 'synthetic-selected-app', 'app_secret': 'synthetic-selected-secret'}
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'synthetic-feishu-notice-token', 'expire': 7200})
        return httpx.Response(200, json={'code': 0, 'data': {'message_id': 'synthetic-approval-card'}})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(**{**kwargs, 'transport': httpx.MockTransport(provider)}))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        await approval_service._notify_pending_approval(db, await db.get(Agent, seed['agent_id']), await db.get(ApprovalRequest, approval_id))
        await db.commit()
    if channel == 'feishu' and configured:
        assert len(calls) == 2
        assert str(approval_id) in calls[-1].content.decode()
    else:
        assert calls == [], 'Foreign-provider and disabled credentials must never be sent to the Feishu API'
