"""Local denials and recoverable returns retain exact authority and execution facts."""
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from app.core.execution_context import ExecutionPrincipal
from app.database import get_db, tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import ApprovalRequest
from app.models.capability_policy import CapabilityPolicy
from app.models.chat_session import ChatSession
from app.models.local_agent_channel import LocalAgentChannelMessage
from app.models.user import User
from app.services import local_agent_channel_service as channel
from tests.integration.test_local_agent_channel_protocol import _seed, _grant_local_capabilities

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.fixture
async def delegation(owner_sessionmaker):
    tenant, user, target, context = await _seed(owner_sessionmaker)
    source, parent = uuid4(), uuid4()
    principal = ExecutionPrincipal(tenant_id=tenant, source_agent_id=source,
        requester_user_id=user, root_session_id=str(parent))
    async with tenant_scoped_session(tenant, session_factory=owner_sessionmaker) as db:
        await _grant_local_capabilities(db, tenant_id=tenant, agent_id=target, execute_requires_approval=True)
        db.add(Agent(id=source, tenant_id=tenant, creator_id=user, owner_user_id=user, sponsor_user_id=user,
            name='Original source employee', agent_type='worker', status='running'))
        await db.flush()
        db.add(ChatSession(id=parent, tenant_id=tenant, agent_id=source, user_id=user, title='Original source work',
            source_channel='web', session_kind='human_chat', actor_type='user', runtime_source='web_chat',
            visibility_scope='direct_user', listed_surface='chat'))
        await db.flush()
        await channel.mark_channel_ready(db, context=context, runtime_kind='codex',
            capabilities={'execute': True, 'event_stream': True, 'result_report': True})
        work = await channel.create_channel_session(db, tenant_id=tenant, owner_user_id=user, actor_user_id=user,
            source_agent_id=target, source='a2a', title='Local delegated work')
        request = await channel.enqueue_channel_message(db, session_id=UUID(str(work['id'])), owner_user_id=user,
            sender_user_id=user, sender_agent_id=source, content='Execute only after exact approval.', idempotency_key='approval-return',
            metadata={'source': 'a2a', 'execution_target': 'local_agent', 'sender_agent_id': str(source),
                'target_agent_id': str(target), 'target_owner_user_id': str(user), 'parent_session_id': str(parent),
                'execution_principal': principal.to_evidence()})
    return {'tenant_id': tenant, 'user_id': user, 'agent_id': target, 'selected': context, 'source': source,
        'parent': parent, 'principal': principal, 'work_id': UUID(str(work['id'])), 'message_id': UUID(request['id']),
        'approval_id': UUID(request['approval_id']), 'request_hash': request['request_hash']}


@pytest.fixture
async def approval_api(delegation, app_user_sessionmaker):
    from app.api import agents, local_agent_channel
    from app.core.security import create_access_token
    seed = delegation
    app = FastAPI()
    app.include_router(agents.router)
    app.include_router(local_agent_channel.router)
    async def database():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            yield db
    app.dependency_overrides[get_db] = database
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, seed['user_id'])
        token = create_access_token(str(user.id), user.role, str(user.tenant_id))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local',
        headers={'Authorization': 'Bearer ' + token}) as client:
        yield seed, client


@pytest.mark.parametrize('decision', ['reject', 'policy_changed'])
async def test_denial_is_terminal_and_returns_to_the_exact_source(approval_api, app_user_sessionmaker, decision):
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    seed, api = approval_api
    if decision == 'policy_changed':
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.agent_id == seed['agent_id'],
                CapabilityPolicy.capability == 'local_agent.execute'))
            policy.allowed = False
            await db.commit()
    path = f"/agents/{seed['agent_id']}/approvals/{seed['approval_id']}/resolve"
    action = 'reject' if decision == 'reject' else 'approve'
    response = await api.post(path, json={'action': action})
    assert response.status_code == 200, response.text
    repeated = await api.post(path, json={'action': action})
    assert repeated.status_code == 400 and repeated.json()['detail'] == 'Approval already resolved'
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        approval = await db.get(ApprovalRequest, seed['approval_id'])
        await channel.apply_local_agent_action_approval(db, approval=approval)
        await db.commit()
        view = await channel.get_a2a_channel_message_status(db, message_id=seed['message_id'],
            principal=seed['principal'], source_agent_id=seed['source'])
        assert view['status'] == 'rejected' and view['terminal'] and not view['retryable']
        assert view['execution_state'] == 'not_dispatched' and view['next_action'] == 'revise_original_work'
        assert view['approval']['status'] == ('rejected' if decision == 'reject' else 'approved')
        assert view['approval']['execution_status'] == ('rejected' if decision == 'reject' else 'failed')
        assert view['approval']['resolved_by'] == str(seed['user_id'])
        notices = list(await db.scalars(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(seed['message_id']))))
        notice, = notices
        assert notice.terminal_status == 'rejected' and notice.parent_session_id == seed['parent']
        assert notice.parent_agent_id == seed['source'] and notice.parent_user_id == seed['user_id']
        assert view['source_delivery']['notification_id'] == str(notice.id)
        assert await channel.poll_pending_channel_messages(db, context=seed['selected']) == []


@pytest.mark.parametrize('stage', ['waiting_approval', 'pending', 'rejected'])
@pytest.mark.parametrize('operation', ['ack', 'event', 'result'])
async def test_new_contract_callbacks_cannot_invent_dispatch(delegation, app_user_sessionmaker, stage, operation):
    from app.services.approval_service import ApprovalService
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        if stage != 'waiting_approval':
            user = await db.get(User, seed['user_id'])
            await ApprovalService().resolve_approval(db, seed['approval_id'], user, 'approve' if stage == 'pending' else 'reject')
        with pytest.raises(HTTPException) as refused:
            if operation == 'ack':
                await channel.ack_channel_message(db, context=seed['selected'], message_id=seed['message_id'])
            elif operation == 'event':
                await channel.record_channel_event(db, context=seed['selected'], session_id=seed['work_id'],
                    message_id=seed['message_id'], event_type='text', payload={'text': 'Unrequested bound progress'})
            else:
                await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
                    message_id=seed['message_id'], result_status='completed', output='Unrequested completion')
        assert refused.value.status_code == 409
        await db.rollback()
        event = await channel.record_channel_event(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=None, event_type='text', payload={'text': 'Independent authorized analysis remains available.'})
        assert event['payload']['text'] == 'Independent authorized analysis remains available.'


@pytest.mark.parametrize('transport', ['poll', 'online_push'])
async def test_source_route_change_before_dispatch_withholds_the_original_request(
    delegation, app_user_sessionmaker, monkeypatch, transport,
):
    from app.api.local_agent_channel import channel_ws_manager
    from app.services.approval_service import ApprovalService

    seed = delegation
    sent = []

    async def connected(context):
        assert context.connection_id == seed['selected'].connection_id
        return True

    async def send(context, payload):
        sent.append(payload)
        return True

    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, seed['user_id'])
        await ApprovalService().resolve_approval(db, seed['approval_id'], user, 'approve')
        message = await db.get(LocalAgentChannelMessage, seed['message_id'])
        original_content, original_metadata = message.content, dict(message.metadata_json)
        source = await db.get(ChatSession, seed['parent'])
        source.agent_id = seed['agent_id']
        await db.commit()
        monkeypatch.setattr(channel_ws_manager, 'has_connection', connected)
        monkeypatch.setattr(channel_ws_manager, 'send_to_connection', send)
        if transport == 'poll':
            assert await channel.poll_pending_channel_messages(db, context=seed['selected']) == []
        else:
            await channel.dispatch_channel_message(db, message_id=seed['message_id'], owner_user_id=seed['user_id'])
            assert sent == []
        await db.refresh(message)
        assert message.status == 'rejected' and message.delivery_attempt_count == 0
        assert message.content == original_content
        assert message.metadata_json['execution_principal'] == original_metadata['execution_principal']
        origin = await channel._dispatch_origin(db, message)
        assert channel._execution_state(message, origin) == 'not_dispatched'
        assert message.metadata_json['source_delivery']['status'] == 'needs_reconciliation'
        source.agent_id = seed['source']
        await db.commit()
        assert await channel.poll_pending_channel_messages(db, context=seed['selected']) == []
        assert sent == []


async def test_source_route_loss_holds_delivery_without_dropping_the_actual_result(delegation, app_user_sessionmaker):
    from app.services.approval_service import ApprovalService
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, seed['user_id'])
        await ApprovalService().resolve_approval(db, seed['approval_id'], user, 'approve')
        assert await channel.poll_pending_channel_messages(db, context=seed['selected'])
        source = await db.get(ChatSession, seed['parent'])
        source.agent_id = seed['agent_id']
        await db.commit()
        result = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=seed['message_id'], result_status='completed', output='Exact authorized result survives source loss.')
        assert result['status'] == 'completed'
        assert result['source_delivery']['status'] == 'needs_reconciliation'
        assert result['source_delivery']['recovery_action'] == 'reconcile_original_source'
        await db.refresh(source)
        source.agent_id = seed['source']
        await db.commit()
        replay = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=seed['message_id'], result_status='failed', output='Duplicate must not replace the original bytes.')
        assert replay['idempotent_replay'] and replay['message']['result'] == 'Exact authorized result survives source loss.'
        assert replay['source_delivery']['notification_id']


async def test_stale_approval_frame_cannot_revive_cancelled_work(delegation, app_user_sessionmaker):
    from app.services.approval_service import ApprovalService
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as stale:
        cached = await stale.get(LocalAgentChannelMessage, seed['message_id'])
        assert cached.status == 'waiting_approval'
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as fresh:
            await channel.cancel_channel_message(fresh, tenant_id=seed['tenant_id'], session_id=seed['work_id'],
                message_id=seed['message_id'], owner_user_id=seed['user_id'], actor_user_id=seed['user_id'],
                request_hash=seed['request_hash'], idempotency_key='before-approval')
        owner = await stale.get(User, seed['user_id'])
        resolved = await ApprovalService().resolve_approval(stale, seed['approval_id'], owner, 'approve')
        assert resolved.status == 'approved'
        await stale.refresh(cached)
        assert cached.status == 'cancelled'


async def test_legacy_unknown_reporting_is_retained_without_forging_an_approval(delegation, app_user_sessionmaker):
    from app.models.invocation_span import InvocationSpan
    from app.services.approval_service import ApprovalService
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        owner = await db.get(User, seed['user_id'])
        await ApprovalService().resolve_approval(db, seed['approval_id'], owner, 'reject')
        origin = await db.scalar(select(InvocationSpan).where(InvocationSpan.request_id == seed['message_id'],
            InvocationSpan.span_type == 'remote_action'))
        origin.metadata_json = {key: value for key, value in origin.metadata_json.items() if key != 'dispatch_protocol'}
        await db.commit()
        result = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=seed['message_id'], result_status='completed', output='Actual report from an ambiguous legacy delivery.')
        assert result['status'] == 'completed'
        source = await channel.get_a2a_channel_message_status(db, message_id=seed['message_id'],
            principal=seed['principal'], source_agent_id=seed['source'])
        assert source['execution_state'] == 'reported_completed'
        assert source['approval']['status'] == 'rejected' and source['approval']['execution_status'] == 'rejected'


async def test_source_storage_failure_preserves_result_and_user_can_retry_without_execution(
    approval_api, app_user_sessionmaker, monkeypatch,
):
    from sqlalchemy import text
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    seed, api = approval_api
    response = await api.post(f"/agents/{seed['agent_id']}/approvals/{seed['approval_id']}/resolve", json={'action': 'approve'})
    assert response.status_code == 200, response.text
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        assert await channel.poll_pending_channel_messages(db, context=seed['selected'])
        async def unavailable(db, _notification):
            await db.execute(text('SELECT * FROM nonexistent_m3e_publication_probe'))
        with monkeypatch.context() as failure:
            failure.setattr(channel, 'enqueue_completion_notification', unavailable)
            result = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
                message_id=seed['message_id'], result_status='completed', output='Exact result despite publication storage failure.')
        assert result['source_delivery']['status'] == 'needs_reconciliation'
        assert result['source_delivery']['reason'] == 'source_storage_unavailable'
        await db.refresh(await db.get(LocalAgentChannelMessage, seed['message_id']))
        assert result['message']['result'] == 'Exact result despite publication storage failure.'
    view = await api.get(f"/local-agents/sessions/{seed['work_id']}/requests")
    projected, = view.json()['requests']
    assert projected['source_delivery']['status'] == 'needs_reconciliation' and projected['can_retry_source_return']
    path = f"/local-agents/sessions/{seed['work_id']}/messages/{seed['message_id']}/source-return/retry"
    body = {'expected_user_id': str(seed['user_id']), 'expected_tenant_id': str(seed['tenant_id']), 'request_hash': seed['request_hash']}
    returned = await api.post(path, json=body)
    assert returned.status_code == 200 and not returned.json()['reexecuted'], returned.text
    assert returned.json()['source_delivery']['status'] == 'queued'
    again = await api.post(path, json=body)
    assert again.status_code == 200 and again.json()['source_delivery'] == returned.json()['source_delivery']
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, seed['message_id'])
        assert message.delivery_attempt_count == 1 and message.result == 'Exact result despite publication storage failure.'
        notices = list(await db.scalars(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(message.id))))
        assert len(notices) == 1 and notices[0].parent_session_id == seed['parent']


async def test_denial_reaches_original_parent_entry_and_exact_result_reader(
    delegation, app_user_sessionmaker, owner_sessionmaker, monkeypatch, tmp_path,
):
    import json
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.approval_service import ApprovalService
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.tools.handlers import context_resources
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
    from tests.services.test_runtime_notification_outbox import _clear_outbox
    await _clear_outbox(owner_sessionmaker)
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        owner = await db.get(User, seed['user_id'])
        await ApprovalService().resolve_approval(db, seed['approval_id'], owner, 'reject')
    calls = []
    async def model_entry(**kwargs):
        calls.append(kwargs)
        return {'run_id': str(uuid4()), 'status': 'queued'}
    monkeypatch.setattr('app.services.agent_session_continuation.start_web_chat_run', model_entry)
    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    delivered = await service.drain_once(worker_id='local-approval-denial-return')
    assert delivered['delivered'] >= 1 and delivered['retried'] == 0
    assert len(calls) == 1
    assert calls[0]['agent'].id == seed['source'] and calls[0]['user'].id == seed['user_id']
    assert calls[0]['session'].id == seed['parent'] and 'runtime-result://' in calls[0]['content']
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == seed['parent'])))
        refs = {ref for event in events for ref in (event.metadata_json or {}).get('result_refs', [])}
    assert len(refs) == 1
    monkeypatch.setattr(context_resources, 'async_session', app_user_sessionmaker)
    page = json.loads(await context_resources.read_runtime_result(ToolExecutionRequest(tool_name='read_runtime_result',
        arguments={'result_ref': refs.pop(), 'offset': 0, 'limit': 12000}, context=ToolExecutionContext(
            agent_id=seed['source'], user_id=seed['user_id'], tenant_id=str(seed['tenant_id']),
            workspace=tmp_path, session_id=str(seed['parent'])))))
    assert page['status'] == 'ok' and page['complete']
    payload = json.loads(page['content'])
    assert payload['summary'] == 'Owner rejected this Local Agent action.'
    assert payload['metadata']['approval']['status'] == 'rejected'
    assert payload['metadata']['approval']['resolved_by'] == str(seed['user_id'])


@pytest.mark.parametrize('field', ['expected_user_id', 'expected_tenant_id', 'request_hash'])
async def test_source_return_retry_rejects_stale_control_scope(approval_api, app_user_sessionmaker, field):
    from app.services.approval_service import ApprovalService
    seed, api = approval_api
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        owner = await db.get(User, seed['user_id'])
        await ApprovalService().resolve_approval(db, seed['approval_id'], owner, 'reject')
    body = {'expected_user_id': str(seed['user_id']), 'expected_tenant_id': str(seed['tenant_id']), 'request_hash': seed['request_hash']}
    body[field] = 'f' * 64 if field == 'request_hash' else str(uuid4())
    response = await api.post(f"/local-agents/sessions/{seed['work_id']}/messages/{seed['message_id']}/source-return/retry", json=body)
    assert response.status_code == 409, response.text


async def test_caller_source_receipt_metadata_is_not_publication_evidence(delegation, app_user_sessionmaker):
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, seed['message_id'])
        message.metadata_json = {**message.metadata_json, 'source_delivery_receipt': {'schema': 'hive.local_source_delivery.v1',
            'message_id': str(message.id), 'request_hash': message.request_hash, 'status': 'queued'}}
        await db.commit()
        projected = await channel.list_channel_requests(db, tenant_id=seed['tenant_id'], session_id=seed['work_id'],
            owner_user_id=seed['user_id'], actor_user_id=seed['user_id'])
        request, = projected['requests']
        assert request['source_delivery'] is None and not request['can_retry_source_return']


async def test_mounted_original_work_can_retry_retained_result_without_reexecution(
    delegation, app_user_sessionmaker, tmp_path,
):
    from app.api import local_agent_channel
    from app.core.security import create_access_token
    from app.services.approval_service import ApprovalService
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        owner = await db.get(User, seed['user_id'])
        await ApprovalService().resolve_approval(db, seed['approval_id'], owner, 'approve')
        await channel.poll_pending_channel_messages(db, context=seed['selected'])
        parent = await db.get(ChatSession, seed['parent'])
        parent.agent_id = seed['agent_id']
        await db.commit()
        result = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=seed['message_id'], result_status='completed', output='Retained exact result for original-work recovery.')
        assert result['source_delivery']['status'] == 'needs_reconciliation'
        parent.agent_id = seed['source']
        await db.commit()
        token = create_access_token(str(owner.id), owner.role, str(owner.tenant_id))
    web = FastAPI()
    web.include_router(local_agent_channel.router, prefix='/api')
    async def database():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            yield db
    web.dependency_overrides[get_db] = database
    await _exercise_dom_bridge(web, seed, tmp_path, entry_file='e2e/local-source-return-native.tsx',
        extra_env={'HIVE_M3E_SESSION_ID': str(seed['work_id']), 'HIVE_M3E_TOKEN': token})
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, seed['message_id'])
        assert message.delivery_attempt_count == 1 and message.result == 'Retained exact result for original-work recovery.'
        notices = list(await db.scalars(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(message.id))))
        notice, = notices
        assert notice.parent_session_id == seed['parent'] and notice.parent_agent_id == seed['source']


async def test_retry_of_unreleased_work_does_not_publish_caller_metadata_as_output(approval_api, app_user_sessionmaker, monkeypatch):
    import json
    from sqlalchemy import text
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_result import RuntimeResultObject
    seed, api = approval_api
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, seed['message_id'])
        message.metadata_json = {**message.metadata_json, 'report': {'artifacts': [{'path': 'workspace/unexecuted-output.md'}]},
            'execution_receipt': {'result_refs': ['workspace/unexecuted-reference.md']}}
        await db.commit()
    async def unavailable(db, _notification):
        await db.execute(text('SELECT * FROM nonexistent_m3e_publication_probe'))
    with monkeypatch.context() as failure:
        failure.setattr(channel, 'enqueue_completion_notification', unavailable)
        response = await api.post(f"/agents/{seed['agent_id']}/approvals/{seed['approval_id']}/resolve", json={'action': 'reject'})
        assert response.status_code == 200, response.text
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        view = await channel.get_a2a_channel_message_status(db, message_id=seed['message_id'],
            principal=seed['principal'], source_agent_id=seed['source'])
        assert view['receipt']['result_refs'] == []
    body = {'expected_user_id': str(seed['user_id']), 'expected_tenant_id': str(seed['tenant_id']), 'request_hash': seed['request_hash']}
    retried = await api.post(f"/local-agents/sessions/{seed['work_id']}/messages/{seed['message_id']}/source-return/retry", json=body)
    assert retried.status_code == 200, retried.text
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        notice = await db.scalar(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(seed['message_id'])))
        result = await db.get(RuntimeResultObject, notice.result_object_id)
        assert json.loads(result.payload_bytes)['artifacts'] == []
