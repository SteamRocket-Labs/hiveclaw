"""Cancellation reports exact queue facts in the authenticated original work."""

from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
import httpx
import pytest

from app.database import get_db, tenant_scoped_session
from tests.integration import test_m3a_local_employee_control_postgres as _routing

local_work = _routing.local_work
pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.fixture
async def control_api(local_work, app_user_sessionmaker):
    from app.api import local_agent_channel as api
    from app.core.security import create_access_token
    from app.models.user import User

    seed = local_work
    app = FastAPI()
    app.include_router(api.router)

    async def scoped_db():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            yield db

    app.dependency_overrides[get_db] = scoped_db
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, seed['user_id'])
        token = create_access_token(str(user.id), user.role, str(user.tenant_id))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local',
        headers={'Authorization': 'Bearer ' + token}) as client:
        yield seed, client


async def test_cancel_pending_request_prevents_dispatch_and_ignores_a_forged_completion(
    control_api, app_user_sessionmaker,
):
    from app.models.local_agent_channel import LocalAgentChannelMessage
    from app.services import local_agent_channel_service as channel

    seed, api = control_api
    message_id = UUID(seed['message_id'])
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, message_id)
        request_hash = message.request_hash
    path = f"/agents/{seed['agent_id']}/local-agent/sessions/{seed['work_id']}/messages/{message_id}/cancel"
    command = {'expected_user_id': str(seed['user_id']), 'expected_tenant_id': str(seed['tenant_id']),
        'request_hash': request_hash, 'idempotency_key': 'cancel-' + uuid4().hex}
    projected = await api.get(f"/local-agents/sessions/{seed['work_id']}/requests")
    assert projected.status_code == 200, projected.text
    pending, = projected.json()['requests']
    assert pending['can_cancel'] and pending['execution_state'] == 'not_dispatched'
    response = await api.post(path, json=command)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt['status'] == 'cancelled' and receipt['execution_state'] == 'not_dispatched'
    replay = await api.post(path, json=command)
    assert replay.status_code == 200 and replay.json() == receipt
    projected = await api.get(f"/local-agents/sessions/{seed['work_id']}/requests")
    cancelled, = projected.json()['requests']
    assert cancelled['status'] == 'cancelled' and not cancelled['can_cancel'] and cancelled['control'] == receipt
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        assert await channel.poll_pending_channel_messages(db, context=seed['selected']) == []
        message = await db.get(LocalAgentChannelMessage, message_id)
        assert message.status == 'cancelled' and message.delivery_attempt_count == 0 and message.delivered_at is None
        with pytest.raises(HTTPException) as refused:
            await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
                message_id=message_id, result_status='completed', output='Unrequested completion after cancellation.')
        assert refused.value.status_code == 409
        await db.refresh(message)
        assert message.status == 'cancelled' and message.result != 'Unrequested completion after cancellation.'


async def _cancel(db, seed, key='withdraw'):
    from app.models.local_agent_channel import LocalAgentChannelMessage
    from app.services import local_agent_channel_service as channel
    message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
    return await channel.cancel_channel_message(db, tenant_id=seed['tenant_id'], session_id=seed['work_id'],
        message_id=message.id, owner_user_id=seed['user_id'], actor_user_id=seed['user_id'],
        request_hash=message.request_hash, idempotency_key=key)


@pytest.mark.parametrize('provenance', ['legacy', 'tampered', 'delivered'])
async def test_ambiguous_execution_is_held_and_late_original_result_remains_visible(
    local_work, app_user_sessionmaker, provenance,
):
    from sqlalchemy import select
    from app.models.invocation_span import InvocationSpan
    from app.models.local_agent_channel import LocalAgentChannelMessage
    from app.services import local_agent_channel_service as channel

    seed = local_work
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        if provenance == 'delivered':
            assert await channel.poll_pending_channel_messages(db, context=seed['selected'])
        else:
            span = await db.scalar(select(InvocationSpan).where(InvocationSpan.request_id == message.id))
            if provenance == 'legacy':
                span.metadata_json = {key: value for key, value in span.metadata_json.items() if key != 'dispatch_protocol'}
                message.metadata_json = {**message.metadata_json, 'dispatch_protocol': 'hive.local_message_leased_dispatch.v1'}
            else:
                span.input_hash = 'f' * 64
            await db.commit()
        result = await _cancel(db, seed)
        receipt = result['receipt']
        assert receipt['status'] == 'needs_reconciliation' and receipt['execution_state'] == 'unknown'
        assert await channel.poll_pending_channel_messages(db, context=seed['selected']) == []
        accepted = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=message.id, result_status='completed', output='Actual late original result.')
        assert accepted['status'] == 'completed'
        assert (await _cancel(db, seed))['receipt'] == receipt
        view = await channel.list_channel_requests(db, tenant_id=seed['tenant_id'], session_id=seed['work_id'],
            owner_user_id=seed['user_id'], actor_user_id=seed['user_id'])
        request, = view['requests']
        assert request['execution_state'] == 'reported_completed' and request['next_action'] == 'review_late_result'


async def test_cancelled_request_cannot_inject_progress(local_work, app_user_sessionmaker):
    from app.services import local_agent_channel_service as channel
    seed = local_work
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        assert (await _cancel(db, seed))['receipt']['status'] == 'cancelled'
        with pytest.raises(HTTPException) as refused:
            await channel.record_channel_event(db, context=seed['selected'], session_id=seed['work_id'],
                message_id=UUID(seed['message_id']), event_type='text', payload={'text': 'Forged progress'})
        assert refused.value.status_code == 409


@pytest.mark.parametrize('field', ['expected_user_id', 'expected_tenant_id', 'request_hash'])
async def test_stale_account_tenant_or_request_cannot_cancel(control_api, app_user_sessionmaker, field):
    from app.models.local_agent_channel import LocalAgentChannelMessage
    seed, api = control_api
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        body = {'expected_user_id': str(seed['user_id']), 'expected_tenant_id': str(seed['tenant_id']),
            'request_hash': message.request_hash, 'idempotency_key': 'wrong-scope'}
    body[field] = 'f' * 64 if field == 'request_hash' else str(uuid4())
    response = await api.post(f"/local-agents/sessions/{seed['work_id']}/messages/{seed['message_id']}/cancel", json=body)
    assert response.status_code == 409, response.text
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        assert message.status == 'pending'


async def test_cancel_and_dispatch_share_the_same_row_lock(local_work, app_user_sessionmaker):
    import asyncio
    from app.services import local_agent_channel_service as channel
    seed = local_work
    async def cancel():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            return await _cancel(db, seed)
    async def poll():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            return await channel.poll_pending_channel_messages(db, context=seed['selected'])
    controlled, delivered = await asyncio.gather(cancel(), poll())
    receipt = controlled['receipt']
    assert (not delivered and receipt['execution_state'] == 'not_dispatched') or (
        len(delivered) == 1 and receipt['execution_state'] == 'unknown')
    left, right = await asyncio.gather(cancel(), cancel())
    assert left['receipt'] == right['receipt'] == receipt


@pytest.mark.parametrize('operation', ['cancel_after_dispatch', 'result_after_cancel', 'event_after_cancel'])
async def test_locked_control_uses_current_row_instead_of_cached_state(local_work, app_user_sessionmaker, operation):
    from app.models.local_agent_channel import LocalAgentChannelMessage
    from app.services import local_agent_channel_service as channel
    seed = local_work
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as stale:
        cached = await stale.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        assert cached.status == 'pending'
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as fresh:
            if operation == 'cancel_after_dispatch':
                assert await channel.poll_pending_channel_messages(fresh, context=seed['selected'])
            else:
                assert (await _cancel(fresh, seed))['receipt']['status'] == 'cancelled'
        if operation == 'cancel_after_dispatch':
            result = await _cancel(stale, seed)
            assert result['receipt']['execution_state'] == 'unknown'
        else:
            with pytest.raises(HTTPException) as refused:
                if operation == 'result_after_cancel':
                    await channel.record_channel_result(stale, context=seed['selected'], session_id=seed['work_id'],
                        message_id=cached.id, result_status='completed', output='Stale result')
                else:
                    await channel.record_channel_event(stale, context=seed['selected'], session_id=seed['work_id'],
                        message_id=cached.id, event_type='text', payload={'text': 'Stale progress'})
            assert refused.value.status_code == 409


async def test_later_approval_does_not_revive_a_withdrawn_request(local_work, app_user_sessionmaker):
    from app.models.audit import ApprovalRequest
    from app.models.user import User
    from app.services import local_agent_channel_service as channel
    from app.services.approval_service import ApprovalService
    from sqlalchemy import select
    from app.models.capability_policy import CapabilityPolicy
    seed = local_work
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.agent_id == seed['agent_id'],
            CapabilityPolicy.capability == 'local_agent.execute'))
        policy.requires_approval = True
        await db.commit()
        request = await channel.enqueue_channel_message(db, session_id=seed['work_id'], owner_user_id=seed['user_id'],
            sender_user_id=seed['user_id'], content='Approval-bound request.', idempotency_key='needs-approval')
        assert request['status'] == 'waiting_approval'
        waiting = {**seed, 'message_id': request['id']}
        assert (await _cancel(db, waiting))['receipt']['status'] == 'cancelled'
        approval = await db.get(ApprovalRequest, UUID(request['approval_id']))
        assert approval.status == 'pending', 'Withdrawing work must not impersonate an approver'
        owner = await db.get(User, seed['user_id'])
        resolved = await ApprovalService().resolve_approval(db, approval.id, owner, 'approve')
        assert resolved.status == 'approved'
        applied = await channel.apply_local_agent_action_approval(db, approval=resolved)
        assert applied['status'] == 'cancelled'
        assert all(row['id'] != request['id'] for row in await channel.poll_pending_channel_messages(db, context=seed['selected']))


async def test_cancelled_result_error_is_local_to_its_websocket_frame(local_work, app_user_sessionmaker):
    from types import SimpleNamespace
    from fastapi import WebSocketDisconnect
    from app.api import local_agent_channel as api
    from app.services import local_agent_channel_service as channel
    seed = local_work

    class Socket:
        client = SimpleNamespace(host='127.0.0.1')
        headers = {}
        frames = iter([
            {'type': 'result', 'session_id': str(seed['work_id']), 'message_id': seed['message_id'], 'output': 'Cancelled result'},
            {'type': 'ping'},
        ])
        received = []
        async def accept(self):
            pass
        async def receive_json(self):
            try:
                return next(self.frames)
            except StopIteration:
                raise WebSocketDisconnect() from None
        async def send_json(self, payload):
            self.received.append(payload)
        async def close(self, code):
            raise AssertionError(f'A valid connection was closed: {code}')

    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        await _cancel(db, seed)
        ticket = await channel.create_ws_ticket(db, context=seed['selected'])
        socket = Socket()
        await api.local_agent_channel_ws(socket, ticket=ticket['ticket'], db=db)
    error, = [frame for frame in socket.received if frame['type'] == 'error']
    assert error['status_code'] == 409 and error['message_id'] == seed['message_id']
    assert any(frame['type'] == 'pong' for frame in socket.received)


async def test_cancelled_delegation_returns_to_the_exact_source_work(local_work, app_user_sessionmaker):
    from sqlalchemy import select
    from app.core.execution_context import ExecutionPrincipal
    from app.models.chat_session import ChatSession
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.services import local_agent_channel_service as channel
    seed = local_work
    source, parent = seed['other'].agent_id, uuid4()
    principal = ExecutionPrincipal(tenant_id=seed['tenant_id'], source_agent_id=source,
        requester_user_id=seed['user_id'], root_session_id=str(parent))
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        db.add(ChatSession(id=parent, tenant_id=seed['tenant_id'], agent_id=source, user_id=seed['user_id'],
            source_channel='web', session_kind='human_chat', actor_type='user', runtime_source='web_chat',
            visibility_scope='direct_user', listed_surface='chat', title='Original source work'))
        await db.flush()
        queued = await channel.enqueue_channel_message(db, session_id=seed['work_id'], owner_user_id=seed['user_id'],
            sender_user_id=seed['user_id'], sender_agent_id=source, content='Exact delegated work',
            idempotency_key='source-work', metadata={'source': 'a2a', 'execution_target': 'local_agent',
                'sender_agent_id': str(source), 'target_agent_id': str(seed['agent_id']),
                'target_owner_user_id': str(seed['user_id']), 'parent_session_id': str(parent),
                'execution_principal': principal.to_evidence()})
        await _cancel(db, {**seed, 'message_id': queued['id']})
        view = await channel.get_a2a_channel_message_status(db, message_id=UUID(queued['id']),
            principal=principal, source_agent_id=source)
        assert view['status'] == 'cancelled' and view['terminal'] and not view['retryable']
        assert view['source_delivery']['status'] == 'pending'
        outbox = await db.scalar(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == queued['id']))
        assert outbox.parent_session_id == parent and outbox.parent_agent_id == source
        assert outbox.parent_user_id == seed['user_id'] and outbox.terminal_status == 'cancelled'


async def test_request_pages_and_controls_do_not_cross_work_or_reader_scope(local_work, app_user_sessionmaker):
    from app.models.local_agent_channel import LocalAgentChannelMessage
    from app.models.user import User
    from app.services import local_agent_channel_service as channel
    seed = local_work
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        original = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        second = await channel.enqueue_channel_message(db, session_id=seed['work_id'], owner_user_id=seed['user_id'],
            sender_user_id=seed['user_id'], content='A second original request', idempotency_key='page-two')
        args = dict(tenant_id=seed['tenant_id'], session_id=seed['work_id'],
            owner_user_id=seed['user_id'], actor_user_id=seed['user_id'], limit=1)
        newest = await channel.list_channel_requests(db, **args)
        assert [row['id'] for row in newest['requests']] == [second['id']]
        oldest = await channel.list_channel_requests(db, **args, before=UUID(newest['next_cursor']))
        assert [row['id'] for row in oldest['requests']] == [seed['message_id']] and oldest['next_cursor'] is None
        reader = User(id=uuid4(), tenant_id=seed['tenant_id'], username=uuid4().hex,
            email=uuid4().hex + '@example.test', display_name='Read-only colleague', password_hash='synthetic', is_active=True)
        db.add(reader)
        await db.commit()
        with pytest.raises(HTTPException) as refused:
            await channel.cancel_channel_message(db, tenant_id=seed['tenant_id'], session_id=seed['work_id'],
                message_id=original.id, owner_user_id=seed['user_id'], actor_user_id=reader.id,
                request_hash=original.request_hash, idempotency_key='reader-cannot-control')
        assert refused.value.status_code == 403
        with pytest.raises(HTTPException) as missing:
            await channel.cancel_channel_message(db, tenant_id=seed['tenant_id'], session_id=uuid4(),
                message_id=original.id, owner_user_id=seed['user_id'], actor_user_id=seed['user_id'],
                request_hash=original.request_hash, idempotency_key='other-work')
        assert missing.value.status_code == 404


async def test_mounted_original_work_control_uses_real_jwt_http_and_rls(local_work, app_user_sessionmaker, tmp_path):
    from app.api import local_agent_channel as api
    from app.core.security import create_access_token
    from app.models.local_agent_channel import LocalAgentChannelMessage
    from app.models.user import User
    from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge
    seed = local_work
    web = FastAPI()
    web.include_router(api.router, prefix='/api')
    async def database():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            yield db
    web.dependency_overrides[get_db] = database
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        owner = await db.get(User, seed['user_id'])
        token = create_access_token(str(owner.id), owner.role, str(owner.tenant_id))
    await _exercise_dom_bridge(web, seed, tmp_path, entry_file='e2e/local-request-control-native.tsx',
        extra_env={'HIVE_M3B_SESSION_ID': str(seed['work_id']), 'HIVE_M3B_TOKEN': token})
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        assert message.status == 'cancelled' and message.delivery_attempt_count == 0 and message.delivered_at is None
