"""Closing tracking must preserve unknown execution and genuine late reports."""
from uuid import UUID, uuid4

import pytest

from app.database import tenant_scoped_session
from app.models.local_agent_channel import LocalAgentChannelMessage
from app.services import local_agent_channel_service as channel
from tests.integration import test_m3b_local_cancellation_postgres as _controls

local_work = _controls.local_work
control_api = _controls.control_api
pytestmark = pytest.mark.usefixtures('migrated_pg_url')


async def test_original_requester_can_end_tracking_without_claiming_a_host_stop(control_api, app_user_sessionmaker):
    seed, api = control_api
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        assert await channel.poll_pending_channel_messages(db, context=seed['selected'])
        await _controls._cancel(db, seed)
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        command = {'expected_user_id': str(seed['user_id']), 'expected_tenant_id': str(seed['tenant_id']),
            'request_hash': message.request_hash, 'idempotency_key': 'abandon-' + uuid4().hex}
    path = f"/local-agents/sessions/{seed['work_id']}/messages/{seed['message_id']}/abandon"
    response = await api.post(path, json=command)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt['status'] == 'abandoned' and receipt['execution_state'] == 'unknown'
    assert receipt['actor_user_id'] == str(seed['user_id'])
    assert (await api.post(path, json=command)).json() == receipt
    projected = (await api.get(f"/local-agents/sessions/{seed['work_id']}/requests")).json()
    request, = projected['requests']
    assert request['tracking_state'] == 'abandoned' and request['execution_state'] == 'unknown'
    assert not request['can_cancel'] and not request['can_abandon']
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        assert message.status == 'needs_reconciliation' and message.completed_at is None
        assert await channel.poll_pending_channel_messages(db, context=seed['selected']) == []
        result = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=message.id, result_status='completed', output='Actual late report after tracking ended.')
        assert result['status'] == 'completed'
    request, = (await api.get(f"/local-agents/sessions/{seed['work_id']}/requests")).json()['requests']
    assert request['tracking_state'] == 'abandoned' and request['execution_state'] == 'reported_completed'
    assert request['next_action'] == 'review_late_result' and not request['can_cancel']


@pytest.mark.parametrize('field', ['expected_user_id', 'expected_tenant_id', 'request_hash'])
async def test_stale_scope_cannot_end_tracking(control_api, app_user_sessionmaker, field):
    seed, api = control_api
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        await channel.poll_pending_channel_messages(db, context=seed['selected'])
        await _controls._cancel(db, seed)
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        command = {'expected_user_id': str(seed['user_id']), 'expected_tenant_id': str(seed['tenant_id']),
            'request_hash': message.request_hash, 'idempotency_key': 'wrong-scope'}
    command[field] = 'f' * 64 if field == 'request_hash' else str(uuid4())
    response = await api.post(f"/local-agents/sessions/{seed['work_id']}/messages/{seed['message_id']}/abandon", json=command)
    assert response.status_code == 409, response.text
    request, = (await api.get(f"/local-agents/sessions/{seed['work_id']}/requests")).json()['requests']
    assert request['tracking_state'] == 'open' and request['can_abandon']


async def test_caller_metadata_cannot_close_tracking_or_abandon_a_request_still_queued(control_api, app_user_sessionmaker):
    seed, api = control_api
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        message.metadata_json = {**message.metadata_json, 'disposition': {'status': 'abandoned'}, 'tracking_state': 'abandoned'}
        await db.commit()
        command = {'expected_user_id': str(seed['user_id']), 'expected_tenant_id': str(seed['tenant_id']),
            'request_hash': message.request_hash, 'idempotency_key': 'not-held'}
    request, = (await api.get(f"/local-agents/sessions/{seed['work_id']}/requests")).json()['requests']
    assert request['tracking_state'] == 'open' and not request['can_abandon']
    response = await api.post(f"/local-agents/sessions/{seed['work_id']}/messages/{seed['message_id']}/abandon", json=command)
    assert response.status_code == 409, response.text


async def test_abandoned_delegation_and_its_late_result_project_without_resuming_the_parent(
    local_work, app_user_sessionmaker, owner_sessionmaker, monkeypatch, tmp_path,
):
    from sqlalchemy import select
    from app.core.execution_context import ExecutionPrincipal
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    seed = local_work
    source, parent = seed['other'].agent_id, uuid4()
    principal = ExecutionPrincipal(tenant_id=seed['tenant_id'], source_agent_id=source,
        requester_user_id=seed['user_id'], root_session_id=str(parent))
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        db.add(ChatSession(id=parent, tenant_id=seed['tenant_id'], agent_id=source, user_id=seed['user_id'],
            source_channel='web', session_kind='human_chat', actor_type='user', runtime_source='web_chat',
            visibility_scope='direct_user', listed_surface='chat', title='Original delegated source'))
        await db.flush()
        queued = await channel.enqueue_channel_message(db, session_id=seed['work_id'], owner_user_id=seed['user_id'],
            sender_user_id=seed['user_id'], sender_agent_id=source, content='Delegated unknown work', idempotency_key='delegated',
            metadata={'source': 'a2a', 'execution_target': 'local_agent', 'sender_agent_id': str(source),
                'target_agent_id': str(seed['agent_id']), 'target_owner_user_id': str(seed['user_id']),
                'parent_session_id': str(parent), 'execution_principal': principal.to_evidence()})
        await channel.poll_pending_channel_messages(db, context=seed['selected'])
        await _controls._cancel(db, {**seed, 'message_id': queued['id']})
        message = await db.get(LocalAgentChannelMessage, UUID(queued['id']))
        await channel.abandon_channel_message(db, tenant_id=seed['tenant_id'], session_id=seed['work_id'],
            message_id=message.id, owner_user_id=seed['user_id'], actor_user_id=seed['user_id'],
            request_hash=message.request_hash, idempotency_key='end-unknown')
        await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'], message_id=message.id,
            result_status='completed', output='Actual late delegated output.')
        source_view = await channel.get_a2a_channel_message_status(db, message_id=message.id, principal=principal, source_agent_id=source)
        assert source_view['status'] == 'abandoned' and source_view['tracking_state'] == 'abandoned'
        assert source_view['execution_state'] == 'reported_completed' and source_view['result'] == 'Actual late delegated output.'
        assert source_view['terminal'] and not source_view['retryable']
        notices = list(await db.scalars(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == queued['id'])))
        assert {notice.terminal_status for notice in notices} == {'abandoned', 'completed'}
        assert all(notice.delivery_mode == 'session_projection' and notice.parent_session_id == parent
            and notice.parent_agent_id == source and notice.parent_user_id == seed['user_id'] for notice in notices)
        notice_ids = [notice.id for notice in notices]

    async def forbidden_start(**_kwargs):
        pytest.fail('Ended tracking must never automatically start the parent model')
    monkeypatch.setattr('app.services.agent_session_continuation.start_web_chat_run', forbidden_start)
    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    counts = await service.drain_once(worker_id='unknown-disposition-consumer')
    assert counts['delivered'] >= 2 and counts['retried'] == 0
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        notices = list(await db.scalars(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id.in_(notice_ids))))
        assert all(notice.status == 'delivered' and notice.delivery_receipt_json['consumer'] == 'session_projection' for notice in notices)
        events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == parent)))
        result_refs = {ref for event in events for ref in (event.metadata_json or {}).get('result_refs', [])}
        assert len(result_refs) == 2
    import json
    from app.tools.handlers import context_resources
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
    monkeypatch.setattr(context_resources, 'async_session', app_user_sessionmaker)
    payloads = []
    for ref in result_refs:
        result = json.loads(await context_resources.read_runtime_result(ToolExecutionRequest(
            tool_name='read_runtime_result', arguments={'result_ref': ref, 'offset': 0, 'limit': 12000},
            context=ToolExecutionContext(agent_id=source, user_id=seed['user_id'], tenant_id=str(seed['tenant_id']),
                workspace=tmp_path, session_id=str(parent)))))
        assert result['status'] == 'ok' and result['complete']
        payloads.append(json.loads(result['content']))
    assert any(payload['summary'] == 'Actual late delegated output.' for payload in payloads)
    assert all(payload['metadata']['local_work_disposition']['status'] == 'abandoned' for payload in payloads)


async def test_mounted_control_ends_tracking_via_real_jwt_http_and_rls(local_work, app_user_sessionmaker, tmp_path):
    from fastapi import FastAPI
    from app.api import local_agent_channel as api
    from app.core.security import create_access_token
    from app.database import get_db
    from app.models.user import User
    from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge
    seed = local_work
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        await channel.poll_pending_channel_messages(db, context=seed['selected'])
        user = await db.get(User, seed['user_id'])
        token = create_access_token(str(user.id), user.role, str(user.tenant_id))
    web = FastAPI()
    web.include_router(api.router, prefix='/api')
    async def database():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            yield db
    web.dependency_overrides[get_db] = database
    await _exercise_dom_bridge(web, seed, tmp_path, entry_file='e2e/local-unknown-disposition-native.tsx',
        extra_env={'HIVE_M3B_SESSION_ID': str(seed['work_id']), 'HIVE_M3B_TOKEN': token})
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        assert message.status == 'needs_reconciliation' and message.completed_at is None
        receipt = await channel._find_work_disposition(db, message)
        assert receipt['status'] == 'abandoned' and receipt['execution_state'] == 'unknown'


async def test_host_resource_control_does_not_end_another_requesters_business_tracking(control_api, app_user_sessionmaker):
    from app.models.user import User
    seed, api = control_api
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        requester = User(id=uuid4(), tenant_id=seed['tenant_id'], username=uuid4().hex, email=uuid4().hex + '@example.test',
            display_name='Original business requester', password_hash='synthetic', is_active=True)
        db.add(requester)
        await db.flush()
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        message.sender_user_id = requester.id
        await db.commit()
        await channel.poll_pending_channel_messages(db, context=seed['selected'])
        # The host may withdraw its own resource while the business requester remains distinct.
        assert (await _controls._cancel(db, seed))['receipt']['execution_state'] == 'unknown'
        command = {'expected_user_id': str(seed['user_id']), 'expected_tenant_id': str(seed['tenant_id']),
            'request_hash': message.request_hash, 'idempotency_key': 'host-is-not-requester'}
    response = await api.post(f"/local-agents/sessions/{seed['work_id']}/messages/{seed['message_id']}/abandon", json=command)
    assert response.status_code == 403, response.text
    request, = (await api.get(f"/local-agents/sessions/{seed['work_id']}/requests")).json()['requests']
    assert request['tracking_state'] == 'open' and not request['can_abandon'] and request['can_cancel']
