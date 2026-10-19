"""Local employee control preserves the exact selected runner and original work."""

from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
import pytest

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.local_bridge import LocalAgentBridgeConnection
from app.services import local_agent_channel_service as channel
from tests.integration.test_local_agent_channel_protocol import _grant_local_capabilities, _seed

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.fixture
async def local_work(owner_sessionmaker):
    tenant_id, user_id, agent_id, selected = await _seed(owner_sessionmaker)
    other = replace(selected, agent_id=uuid4(), connection_id=uuid4())
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(Agent(id=other.agent_id, tenant_id=tenant_id, name='Other local employee',
            creator_id=user_id, owner_user_id=user_id, sponsor_user_id=user_id,
            agent_type='local_agent', status='running'))
        await db.flush()
        db.add(LocalAgentBridgeConnection(id=other.connection_id, tenant_id=tenant_id, agent_id=other.agent_id,
            user_id=user_id, device_name='Other device', client_kind='codex', device_fingerprint=uuid4().hex,
            token_hash=uuid4().hex, scopes=list(other.scopes), status='active',
            expires_at=channel.utcnow() + timedelta(days=1)))
        for context in (selected, other):
            await _grant_local_capabilities(db, tenant_id=tenant_id, agent_id=context.agent_id)
            ready = await channel.mark_channel_ready(db, context=context, runtime_kind='codex',
                capabilities={'im': True, 'streaming': True, 'attachments': True, 'workspace': True, 'runner': 'codex'})
            assert 'execute' in ready['effective_capabilities']
        work = await channel.create_channel_session(db, tenant_id=tenant_id, owner_user_id=user_id,
            actor_user_id=user_id, source_agent_id=agent_id, source='web', title='Selected original work')
        work_id = UUID(str(work['id']))
        message = await channel.enqueue_channel_message(db, session_id=work_id, owner_user_id=user_id,
            sender_user_id=user_id, sender_agent_id=agent_id, content='Prepare the selected employee report.',
            idempotency_key='selected-work-' + uuid4().hex)
    return {'tenant_id': tenant_id, 'user_id': user_id, 'agent_id': agent_id, 'selected': selected,
        'other': other, 'work_id': work_id, 'message_id': message['id']}


@pytest.mark.parametrize('operation', ['ack', 'event', 'result'])
async def test_another_employee_connection_cannot_settle_the_selected_work(local_work, app_user_sessionmaker, operation):
    seed = local_work
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        assert await channel.poll_pending_channel_messages(db, context=seed['other']) == []
        with pytest.raises(HTTPException) as refused:
            if operation == 'ack':
                await channel.ack_channel_message(db, context=seed['other'], message_id=UUID(seed['message_id']))
            elif operation == 'event':
                await channel.record_channel_event(db, context=seed['other'], session_id=seed['work_id'],
                    message_id=UUID(seed['message_id']), event_type='text', payload={'text': 'Forged progress.'})
            else:
                await channel.record_channel_result(db, context=seed['other'], session_id=seed['work_id'],
                    message_id=UUID(seed['message_id']), result_status='completed', output='Forged completion.')
        assert refused.value.status_code in {403, 404}
        delivered = await channel.poll_pending_channel_messages(db, context=seed['selected'])
        assert [row['id'] for row in delivered] == [seed['message_id']]
        if operation == 'ack':
            assert (await channel.ack_channel_message(db, context=seed['selected'],
                message_id=UUID(seed['message_id'])))['status'] == 'delivered'
        elif operation == 'event':
            accepted = await channel.record_channel_event(db, context=seed['selected'], session_id=seed['work_id'],
                message_id=UUID(seed['message_id']), event_type='text', payload={'text': 'Actual progress.'})
            assert accepted['payload']['text'] == 'Actual progress.'
        else:
            accepted = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
                message_id=UUID(seed['message_id']), result_status='completed', output='Actual completion.')
            assert accepted['status'] == 'completed'


@pytest.mark.parametrize('transport_outcome', ['sent', 'offline', 'unknown'])
async def test_live_api_dispatch_selects_one_runner_after_a_durable_claim(
    local_work, app_user_sessionmaker, monkeypatch, transport_outcome,
):
    from fastapi import FastAPI
    import httpx
    from app.api import local_agent_channel as api
    from app.core.security import create_access_token
    from app.database import get_db
    from app.models.local_agent_channel import LocalAgentChannelMessage
    from app.models.user import User

    seed = local_work

    class Socket:
        def __init__(self):
            self.received, self.observed = [], []

        async def send_json(self, payload):
            self.received.append(payload)
            if payload['type'] == 'message':
                async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
                    message = await db.get(LocalAgentChannelMessage, UUID(payload['message']['id']))
                    self.observed.append((message.status, message.delivery_attempt_count))
                if transport_outcome == 'unknown':
                    raise OSError('The runtime may have received the bytes before disconnecting')

    selected_socket, other_socket = Socket(), Socket()
    manager = api.LocalAgentChannelConnectionManager()
    if transport_outcome != 'offline':
        await manager.connect(context=seed['selected'], websocket=selected_socket)
    await manager.connect(context=seed['other'], websocket=other_socket)
    monkeypatch.setattr(api, 'channel_ws_manager', manager)
    app = FastAPI()
    app.include_router(api.router)

    async def scoped_db():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            yield db

    app.dependency_overrides[get_db] = scoped_db
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, seed['user_id'])
        jwt = create_access_token(str(user.id), user.role, str(user.tenant_id))
    path = f"/agents/{seed['agent_id']}/local-agent/sessions/{seed['work_id']}/messages"
    request = {'content': 'Prepare this exact request.', 'idempotency_key': 'live-request-' + uuid4().hex}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local',
        headers={'Authorization': 'Bearer ' + jwt}) as client:
        response = await client.post(path, json=request)
        assert response.status_code == 201, response.text
        if transport_outcome == 'offline':
            assert selected_socket.received == [] and other_socket.received == []
            async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
                waiting = await db.get(LocalAgentChannelMessage, UUID(response.json()['id']))
                assert waiting.status == 'pending' and waiting.delivery_attempt_count == 0
            await manager.connect(context=seed['selected'], websocket=selected_socket)
        replay = await client.post(path, json=request)
        assert replay.status_code == 201 and replay.json()['id'] == response.json()['id']
    assert other_socket.received == [], 'The other employee must never receive this private work'
    assert [row['message']['id'] for row in selected_socket.received] == [response.json()['id']]
    assert selected_socket.observed == [('delivered', 1)], 'The selected request must be claimed before sending'
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        original = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        assert original.status == 'pending' and original.delivery_attempt_count == 0
        sent = await db.get(LocalAgentChannelMessage, UUID(response.json()['id']))
        assert sent.status == 'delivered' and sent.delivery_attempt_count == 1
        assert sent.metadata_json['last_live_delivery']['status'] == ('unknown' if transport_outcome == 'unknown' else 'sent')


@pytest.mark.parametrize('operation', ['poll', 'ack', 'event', 'result'])
async def test_revoked_connection_cannot_use_its_cached_runtime_context(local_work, owner_sessionmaker,
    app_user_sessionmaker, operation,
):
    from app.services.local_bridge_service import revoke_connection

    seed = local_work
    async with tenant_scoped_session(seed['tenant_id'], session_factory=owner_sessionmaker) as db:
        await revoke_connection(db, connection_id=seed['selected'].connection_id,
            user_id=seed['user_id'], tenant_id=seed['tenant_id'])
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        with pytest.raises(HTTPException) as refused:
            if operation == 'poll':
                await channel.poll_pending_channel_messages(db, context=seed['selected'])
            elif operation == 'ack':
                await channel.ack_channel_message(db, context=seed['selected'], message_id=UUID(seed['message_id']))
            elif operation == 'event':
                await channel.record_channel_event(db, context=seed['selected'], session_id=seed['work_id'],
                    message_id=UUID(seed['message_id']), event_type='text', payload={'text': 'Revoked progress.'})
            else:
                await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
                    message_id=UUID(seed['message_id']), result_status='completed', output='Revoked completion.')
        assert refused.value.status_code in {401, 403}


@pytest.mark.parametrize('narrowing', ['receive_scope', 'execute_policy'])
async def test_dispatch_denial_preserves_the_authorized_result_channel(local_work, owner_sessionmaker,
    app_user_sessionmaker, narrowing,
):
    from sqlalchemy import select
    from app.models.capability_policy import CapabilityPolicy
    from app.models.local_agent_channel import LocalAgentChannelMessage

    seed = local_work
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        assert len(await channel.poll_pending_channel_messages(db, context=seed['selected'])) == 1
        second = await channel.enqueue_channel_message(db, session_id=seed['work_id'], owner_user_id=seed['user_id'],
            sender_user_id=seed['user_id'], sender_agent_id=seed['agent_id'], content='A separate queued request.',
            idempotency_key='second-request-' + uuid4().hex)
    async with tenant_scoped_session(seed['tenant_id'], session_factory=owner_sessionmaker) as db:
        if narrowing == 'receive_scope':
            connection = await db.get(LocalAgentBridgeConnection, seed['selected'].connection_id)
            connection.scopes = [scope for scope in connection.scopes if scope != 'local_agent:receive']
        else:
            policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == seed['tenant_id'],
                CapabilityPolicy.agent_id == seed['agent_id'], CapabilityPolicy.capability == 'local_agent.execute'))
            policy.allowed = False
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        assert await channel.poll_pending_channel_messages(db, context=seed['selected']) == []
        blocked = await db.get(LocalAgentChannelMessage, UUID(second['id']))
        assert blocked.status == 'pending' and blocked.delivery_attempt_count == 0
        reported = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=UUID(seed['message_id']), result_status='completed', output='The authorized prior result.')
        assert reported['status'] == 'completed'


async def test_another_device_for_the_same_employee_cannot_claim_the_prior_request(local_work, owner_sessionmaker,
    app_user_sessionmaker,
):
    from app.models.local_agent_channel import LocalAgentChannelMessage, LocalAgentChannelSession

    seed = local_work
    other = replace(seed['selected'], connection_id=uuid4())
    async with tenant_scoped_session(seed['tenant_id'], session_factory=owner_sessionmaker) as db:
        db.add(LocalAgentBridgeConnection(id=other.connection_id, tenant_id=seed['tenant_id'], agent_id=seed['agent_id'],
            user_id=seed['user_id'], device_name='Replacement device', client_kind='codex', device_fingerprint=uuid4().hex,
            token_hash=uuid4().hex, scopes=list(other.scopes), status='active', expires_at=channel.utcnow() + timedelta(days=1)))
        await db.flush()
        await channel.mark_channel_ready(db, context=other, runtime_kind='codex',
            capabilities={'execute': True, 'event_stream': True, 'result_report': True})
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        assert await channel.poll_pending_channel_messages(db, context=other) == []
        assert len(await channel.poll_pending_channel_messages(db, context=seed['selected'])) == 1
        with pytest.raises(HTTPException) as refused:
            await channel.ack_channel_message(db, context=other, message_id=UUID(seed['message_id']))
        assert refused.value.status_code == 404
        with pytest.raises(HTTPException) as refused:
            await channel.record_channel_event(db, context=other, session_id=seed['work_id'],
                message_id=UUID(seed['message_id']), event_type='text', payload={'text': 'Wrong device progress.'})
        assert refused.value.status_code == 404
        with pytest.raises(HTTPException) as refused:
            await channel.record_channel_result(db, context=other, session_id=seed['work_id'],
                message_id=UUID(seed['message_id']), result_status='completed', output='Wrong device completion.')
        assert refused.value.status_code == 404
        reported = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=UUID(seed['message_id']), result_status='completed', output='Original device completion.')
        assert reported['status'] == 'completed'
        work = await db.get(LocalAgentChannelSession, seed['work_id'])
        work.connection_id = seed['selected'].connection_id
        await db.flush()
        next_request = await channel.enqueue_channel_message(db, session_id=seed['work_id'], owner_user_id=seed['user_id'],
            sender_user_id=seed['user_id'], sender_agent_id=seed['agent_id'], content='Use the explicitly bound device.',
            idempotency_key='bound-device-' + uuid4().hex)
        original = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        assert next_request['capability_snapshot_hash'] == original.capability_snapshot_hash
        assert await channel.poll_pending_channel_messages(db, context=other) == []
        assert [row['id'] for row in await channel.poll_pending_channel_messages(db, context=seed['selected'])] == [next_request['id']]
