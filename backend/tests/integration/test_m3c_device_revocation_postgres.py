"""Device revocation withdraws exact recipients without claiming the host stopped."""
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from app.database import get_db, tenant_scoped_session
from app.models.invocation_span import InvocationSpan
from app.models.local_agent_channel import LocalAgentChannelMessage
from app.services import local_agent_channel_service as channel
from tests.integration import test_m3a_local_employee_control_postgres as _routing

local_work = _routing.local_work

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.fixture
async def device_api(local_work, app_user_sessionmaker):
    from app.api import local_bridge, local_agent_channel
    from app.core.security import create_access_token
    from app.models.user import User
    seed = local_work
    app = FastAPI()
    app.include_router(local_bridge.router)
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


@pytest.mark.parametrize('execution', ['queued', 'delivered', 'legacy'])
async def test_revoke_device_settles_exact_work_and_preserves_another_employee(
    device_api, app_user_sessionmaker, execution,
):
    seed, api = device_api
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        if execution == 'delivered':
            assert await channel.poll_pending_channel_messages(db, context=seed['selected'])
        elif execution == 'legacy':
            origin = await db.scalar(select(InvocationSpan).where(InvocationSpan.request_id == UUID(seed['message_id'])))
            origin.metadata_json = {key: value for key, value in origin.metadata_json.items() if key != 'dispatch_protocol'}
            await db.commit()
        other = await channel.create_channel_session(db, tenant_id=seed['tenant_id'], owner_user_id=seed['user_id'],
            actor_user_id=seed['user_id'], source_agent_id=seed['other'].agent_id, source='web', title='Other employee work')
        other_request = await channel.enqueue_channel_message(db, session_id=UUID(str(other['id'])),
            owner_user_id=seed['user_id'], sender_user_id=seed['user_id'], content='Other employee must remain usable.',
            idempotency_key='other-' + uuid4().hex)
    path = f"/agents/{seed['agent_id']}/local-bridge/connections/{seed['selected'].connection_id}"
    revoked = await api.delete(path)
    assert revoked.status_code == 200, revoked.text
    control = revoked.json().get('work_control')
    assert control is not None, 'Revocation must return a durable original-work control receipt'
    assert control['host_execution_state'] == 'unknown', 'Credential revocation does not prove host termination'
    replay = await api.delete(path)
    assert replay.json() == revoked.json()
    view = await api.get(f"/local-agents/sessions/{seed['work_id']}/requests")
    assert view.status_code == 200, view.text
    projected, = view.json()['requests']
    assert projected['status'] == ('cancelled' if execution == 'queued' else 'needs_reconciliation')
    assert projected['control']['action'] == 'revoke_connection'
    assert projected['control']['actor_user_id'] == str(seed['user_id'])
    assert projected['execution_state'] == ('not_dispatched' if execution == 'queued' else 'unknown')
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        with pytest.raises(HTTPException) as refused:
            await channel.poll_pending_channel_messages(db, context=seed['selected'])
        assert refused.value.status_code == 403
        pending = await db.get(LocalAgentChannelMessage, UUID(other_request['id']))
        assert pending.status == 'pending'
        delivered = await channel.poll_pending_channel_messages(db, context=seed['other'])
        assert [row['id'] for row in delivered] == [other_request['id']]


@pytest.mark.parametrize("operation", ["dispatch", "enqueue"])
async def test_revocation_waits_for_the_same_dispatch_authority_lock(local_work, app_user_sessionmaker, monkeypatch, operation):
    import asyncio
    from app.services import local_bridge_service as bridge
    seed = local_work
    checked, release = asyncio.Event(), asyncio.Event()
    port = "_active_snapshot_for_context" if operation == "dispatch" else "_active_snapshot_for_session"
    original = getattr(channel, port)
    async def checked_context(*args, **kwargs):
        result = await original(*args, **kwargs)
        checked.set()
        await release.wait()
        return result
    monkeypatch.setattr(channel, port, checked_context)
    async def poll():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            if operation == 'dispatch':
                return await channel.poll_pending_channel_messages(db, context=seed['selected'])
            return [await channel.enqueue_channel_message(db, session_id=seed['work_id'], owner_user_id=seed['user_id'],
                sender_user_id=seed['user_id'], content='Work accepted before revocation.', idempotency_key='before-revocation')]
    async def revoke():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            return await bridge.revoke_connection(db, connection_id=seed['selected'].connection_id,
                user_id=seed['user_id'], tenant_id=seed['tenant_id'])
    poll_task = asyncio.create_task(poll())
    revoke_task = None
    try:
        await asyncio.wait_for(checked.wait(), 5)
        revoke_task = asyncio.create_task(revoke())
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(revoke_task), 0.1)
        release.set()
        delivered, receipt = await asyncio.wait_for(asyncio.gather(poll_task, revoke_task), 5)
        if operation == 'dispatch':
            assert [row['id'] for row in delivered] == [seed['message_id']]
            assert receipt['work_control']['request_counts']['needs_reconciliation'] == 1
        else:
            assert len(delivered) == 1 and delivered[0]['id'] != seed['message_id']
            assert receipt['work_control']['request_counts']['cancelled_before_dispatch'] == 2
    finally:
        release.set()
        for task in [poll_task, revoke_task]:
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


async def test_unbound_legacy_work_is_not_attributed_to_a_revoked_device(local_work, app_user_sessionmaker):
    from app.services import local_bridge_service as bridge
    from app.models.local_agent_channel import LocalAgentChannelSession
    seed = local_work
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        message.capability_snapshot_hash = None
        work = await db.get(LocalAgentChannelSession, seed["work_id"])
        work.connection_id, work.channel_id = None, None
        await db.commit()
        receipt = await bridge.revoke_connection(db, connection_id=seed['selected'].connection_id,
            user_id=seed['user_id'], tenant_id=seed['tenant_id'])
        assert receipt['work_control']['request_counts'] == {'cancelled_before_dispatch': 0, 'needs_reconciliation': 0}
        await db.refresh(message)
        assert message.status == 'pending', 'No trusted recipient proof permits attribution to this device'


async def test_wrong_account_or_tenant_cannot_revoke_a_device(device_api):
    seed, api = device_api
    path = f"/agents/{seed['agent_id']}/local-bridge/connections/{seed['selected'].connection_id}"
    for field in ['expected_user_id', 'expected_tenant_id']:
        response = await api.delete(path, params={field: str(uuid4())})
        assert response.status_code == 409, response.text


async def test_manager_revocation_records_the_actual_actor(device_api, app_user_sessionmaker):
    from app.core.security import create_access_token
    from app.models.user import User
    seed, api = device_api
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        manager = User(id=uuid4(), tenant_id=seed['tenant_id'], username=uuid4().hex, email=uuid4().hex + '@example.test',
            display_name='Company manager', password_hash='synthetic', role='org_admin', is_active=True)
        db.add(manager)
        await db.commit()
        token = create_access_token(str(manager.id), manager.role, str(manager.tenant_id))
    response = await api.delete(f"/agents/{seed['agent_id']}/local-bridge/connections/{seed['selected'].connection_id}",
        headers={'Authorization': 'Bearer ' + token}, params={'expected_user_id': str(manager.id),
            'expected_tenant_id': str(seed['tenant_id'])})
    assert response.status_code == 200, response.text
    assert response.json()['work_control']['actor_user_id'] == str(manager.id)
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        span = await db.scalar(select(InvocationSpan).where(InvocationSpan.request_id == UUID(seed['message_id']),
            InvocationSpan.name == 'local_agent.revoke_connection'))
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        assert span.user_id == manager.id and message.sender_user_id == seed['user_id']


async def test_mounted_device_control_uses_real_jwt_http_and_rls(local_work, app_user_sessionmaker, tmp_path):
    from app.api import local_bridge, local_agent_channel
    from app.core.security import create_access_token
    from app.models.user import User
    from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge
    seed = local_work
    web = FastAPI()
    web.include_router(local_bridge.router, prefix='/api')
    web.include_router(local_agent_channel.router, prefix='/api')
    async def database():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            yield db
    web.dependency_overrides[get_db] = database
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, seed['user_id'])
        token = create_access_token(str(user.id), user.role, str(user.tenant_id))
    await _exercise_dom_bridge(web, seed, tmp_path, entry_file='e2e/local-device-control-native.tsx',
        extra_env={'HIVE_M3C_TOKEN': token})
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        assert message.status == 'cancelled' and message.delivery_attempt_count == 0


async def test_revoked_device_cannot_accept_new_work_from_a_still_valid_snapshot(device_api):
    seed, api = device_api
    revoked = await api.delete(f"/agents/{seed['agent_id']}/local-bridge/connections/{seed['selected'].connection_id}")
    assert revoked.status_code == 200, revoked.text
    queued = await api.post(f"/agents/{seed['agent_id']}/local-agent/sessions/{seed['work_id']}/messages",
        json={'content': 'This new request must not bind the revoked device.', 'idempotency_key': 'after-revocation'})
    assert queued.status_code == 409, queued.text
