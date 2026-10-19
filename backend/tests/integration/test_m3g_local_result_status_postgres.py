"""Only protocol terminal results become completion; unresolved reports stay recoverable."""
import asyncio
import json
import socket

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.database import get_db, tenant_scoped_session
from app.models.local_agent_channel import LocalAgentChannelMessage, LocalAgentChannelEvent
from app.models.local_bridge import LocalAgentBridgeConnection
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultObject
from app.models.user import User
from app.services import local_agent_channel_service as channel
from app.services.approval_service import ApprovalService
from app.services.local_bridge_service import hash_secret
from tests.integration import test_m3e_local_approval_return_postgres as local_return

delegation = local_return.delegation
pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.fixture
async def leased(delegation, app_user_sessionmaker):
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        owner = await db.get(User, seed['user_id'])
        await ApprovalService().resolve_approval(db, seed['approval_id'], owner, 'approve')
        assert await channel.poll_pending_channel_messages(db, context=seed['selected'], message_id=seed['message_id'])
    return seed


@pytest.mark.parametrize('reported_status', ['unknown', 'unsupported', 'cancelled', '', 'running'])
async def test_unresolved_report_retains_evidence_without_claiming_completion_or_reexecution(
    leased, app_user_sessionmaker, reported_status,
):
    seed = leased
    output = '' if reported_status == 'unsupported' else 'Exact device observation; execution outcome is unresolved.'
    artifacts = [{'source_ref': 'partial://device-observation'}] if reported_status == 'unknown' else []
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        result = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=seed['message_id'], result_status=reported_status, output=output, artifacts=artifacts)
        assert result['status'] == 'needs_reconciliation' and result['receipt']['status'] == 'needs_reconciliation'
        message = await db.get(LocalAgentChannelMessage, seed['message_id'], populate_existing=True)
        assert message.result == output and message.completed_at is None and message.delivery_lease_expires_at is None
        assert message.metadata_json['report']['reported_status'] == reported_status
        assert await channel.poll_pending_channel_messages(db, context=seed['selected']) == []
        repeated = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=seed['message_id'], result_status=reported_status, output=output, artifacts=artifacts)
        assert repeated['idempotent_replay'] and repeated['receipt'] == result['receipt']
        notices = list(await db.scalars(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(message.id))))
        notice, = notices
        assert notice.terminal_status == 'needs_reconciliation' and notice.parent_session_id == seed['parent']
        if not output:
            stored_result = await db.get(RuntimeResultObject, notice.result_object_id)
            assert 'completed' not in json.loads(stored_result.payload_bytes)['summary']
        status = await channel.get_a2a_channel_message_status(db, message_id=message.id,
            principal=seed['principal'], source_agent_id=seed['source'])
        assert status['execution_state'] == 'unknown' and status['next_action'] == 'inspect_original_runner'
        assert status['receipt']['result_refs'] == [a['source_ref'] for a in artifacts]
        late = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=message.id, result_status='completed', output='Actual later terminal result.')
        assert late['status'] == 'completed' and late['message']['result'] == 'Actual later terminal result.'
        events = list(await db.scalars(select(LocalAgentChannelEvent).where(LocalAgentChannelEvent.message_id == message.id,
            LocalAgentChannelEvent.event_type == 'result').order_by(LocalAgentChannelEvent.sequence)))
        assert len(events) == 2 and events[0].payload_json['reported_status'] == reported_status
        assert events[0].payload_json['output'] == output and events[1].payload_json['status'] == 'completed'
        assert events[0].payload_json['artifacts'] == artifacts
        await db.refresh(message)
        assert message.delivery_attempt_count == 1


@pytest.fixture
async def channel_api(leased, app_user_sessionmaker):
    from app.api import local_agent_channel as api
    seed = leased
    token = 'hb_owned_result_status_probe_' + seed['message_id'].hex
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        connection = await db.get(LocalAgentBridgeConnection, seed['selected'].connection_id)
        connection.token_hash = hash_secret(token)
        await db.commit()
    app = FastAPI()
    app.include_router(api.router)
    async def database():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            yield db
    app.dependency_overrides[get_db] = database
    return seed, app, token


async def test_actual_bridge_http_report_preserves_unknown_status(channel_api):
    seed, app, token = channel_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local',
        headers={'Authorization': 'Bearer ' + token}) as client:
        response = await client.post('/local-bridge/channel/report', json={'session_id': str(seed['work_id']),
            'message_id': str(seed['message_id']), 'status': 'unknown', 'output': 'Actual HTTP observation.'})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'needs_reconciliation'


@pytest.mark.parametrize('wire_status', ['', None, False, 'missing'])
async def test_actual_websocket_preserves_unresolved_values_and_legacy_missing_default(
    channel_api, app_user_sessionmaker, wire_status,
):
    import uvicorn
    import websockets
    seed, app, _token = channel_api
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        ticket = await channel.create_ws_ticket(db, context=seed['selected'])
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.02)
        assert server.started
        async with websockets.connect(f"ws://127.0.0.1:{port}/local-bridge/channel/ws?ticket={ticket['ticket']}") as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert hello['type'] == 'hello'
            frame = {'type': 'result', 'session_id': str(seed['work_id']),
                'message_id': str(seed['message_id']), 'output': 'Exact WS observation.'}
            if wire_status != 'missing':
                frame['status'] = wire_status
            await ws.send(json.dumps(frame))
            ack = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert ack['type'] == 'result_ack'
            assert ack['status'] == ('completed' if wire_status == 'missing' else 'needs_reconciliation')
            await ws.send(json.dumps({'type': 'event', 'session_id': str(seed['work_id']),
                'event_type': 'text', 'payload': {'text': 'Independent reasoning remains usable.'}}))
            assert json.loads(await asyncio.wait_for(ws.recv(), 5))['type'] == 'event_ack'
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


@pytest.mark.parametrize('attested', [True, False])
async def test_source_retry_requires_a_real_unresolved_report_and_never_reexecutes(
    channel_api, app_user_sessionmaker, attested,
):
    from app.core.security import create_access_token
    from app.models.chat_session import ChatSession
    seed, app, _token = channel_api
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, seed['message_id'])
        owner = await db.get(User, seed['user_id'])
        token = create_access_token(str(owner.id), owner.role, str(owner.tenant_id))
        if attested:
            parent = await db.get(ChatSession, seed['parent'])
            parent.agent_id = seed['agent_id']
            await db.commit()
            report = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
                message_id=message.id, result_status='unknown', output='Retained unknown outcome.',
                artifacts=[{'source_ref': 'partial://retained-observation'}])
            assert report['source_delivery']['status'] == 'needs_reconciliation'
            projected = await channel.list_channel_requests(db, tenant_id=seed['tenant_id'], session_id=seed['work_id'],
                owner_user_id=seed['user_id'], actor_user_id=seed['user_id'])
            assert projected['requests'][0]['can_retry_source_return']
            parent.agent_id = seed['source']
        else:
            await channel.cancel_channel_message(db, tenant_id=seed['tenant_id'], session_id=seed['work_id'],
                message_id=message.id, owner_user_id=seed['user_id'], actor_user_id=seed['user_id'],
                request_hash=seed['request_hash'], idempotency_key='hold-without-report')
            message.metadata_json = {**message.metadata_json,
                'report': {'reported_status': 'unknown', 'artifacts': [{'source_ref': 'unexecuted://caller-input'}]},
                'unresolved_result_report': {'schema': 'hive.local_unresolved_report.v1', 'request_hash': seed['request_hash']}}
            await db.commit()
            view = await channel.get_a2a_channel_message_status(db, message_id=message.id,
                principal=seed['principal'], source_agent_id=seed['source'])
            assert view['artifacts'] == []
        await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local',
        headers={'Authorization': 'Bearer ' + token}) as client:
        response = await client.post(f"/local-agents/sessions/{seed['work_id']}/messages/{seed['message_id']}/source-return/retry",
            json={'expected_user_id': str(seed['user_id']), 'expected_tenant_id': str(seed['tenant_id']),
                'request_hash': seed['request_hash']})
        assert response.status_code == (200 if attested else 409), response.text
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, seed['message_id'])
        assert message.status == 'needs_reconciliation' and message.delivery_attempt_count == 1
        if attested:
            notice, = list(await db.scalars(select(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.source_run_id == str(message.id))))
            result = await db.get(RuntimeResultObject, notice.result_object_id)
            body = json.loads(result.payload_bytes)
            assert body['summary'] == 'Retained unknown outcome.'
            assert body['artifacts'] == [{'source_ref': 'partial://retained-observation'}]
