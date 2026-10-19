"""Company limits and current action approval govern the actual dispatch lease."""
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.audit import ApprovalRequest
from app.models.capability_policy import CapabilityPolicy
from app.models.invocation_span import InvocationSpan
from app.models.local_agent_channel import LocalAgentChannelMessage
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.user import User
from app.services import local_agent_channel_service as channel
from app.services.approval_service import ApprovalService
from tests.integration import test_m3e_local_approval_return_postgres as local_return

delegation = local_return.delegation
approval_api = local_return.approval_api

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


async def _policy(db, seed):
    return await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.agent_id == seed['agent_id'],
        CapabilityPolicy.capability == 'local_agent.execute'))


async def _allow_without_approval(db, seed):
    policy = await _policy(db, seed)
    policy.requires_approval = False
    await db.commit()
    original = await db.get(LocalAgentChannelMessage, seed['message_id'])
    owner = await db.get(User, seed['user_id'])
    await ApprovalService().resolve_approval(db, seed['approval_id'], owner, 'approve')
    return original


async def _enqueue(db, seed, key, *, attachments=None):
    original = await db.get(LocalAgentChannelMessage, seed['message_id'])
    return await channel.enqueue_channel_message(db, session_id=seed['work_id'], owner_user_id=seed['user_id'],
        sender_user_id=seed['user_id'], sender_agent_id=seed['source'], content=original.content,
        metadata=original.metadata_json, attachments=attachments, idempotency_key=key)


async def test_company_approval_cannot_be_relaxed_by_employee_allow(delegation, app_user_sessionmaker):
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        await _allow_without_approval(db, seed)
        db.add(CapabilityPolicy(tenant_id=seed['tenant_id'], capability='local_agent.execute',
            allowed=True, requires_approval=True, conditions={}))
        await db.commit()
        queued = await _enqueue(db, seed, 'company-approval')
        assert queued['status'] == 'waiting_approval' and queued['approval_id']
        approval = await db.get(ApprovalRequest, UUID(queued['approval_id']))
        assert approval.details['request_hash'] == queued['request_hash']


@pytest.mark.parametrize('condition,attachments', [
    ({'allowed_sources': ['web']}, []), ({'max_content_chars': 4}, []),
    ({'max_attachments': 0}, [{'path': 'input.txt'}]),
])
@pytest.mark.parametrize('restrictive_layer', ['company', 'employee'])
async def test_company_action_conditions_cannot_be_relaxed_by_employee_allow(
    delegation, app_user_sessionmaker, condition, attachments, restrictive_layer,
):
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        await _allow_without_approval(db, seed)
        permissive = {'allowed_sources': ['a2a', 'web'], 'max_content_chars': 1000, 'max_attachments': 3}
        policy = await _policy(db, seed)
        policy.conditions = condition if restrictive_layer == 'employee' else permissive
        db.add(CapabilityPolicy(tenant_id=seed['tenant_id'], capability='local_agent.execute',
            allowed=True, requires_approval=False, conditions=condition if restrictive_layer == 'company' else permissive))
        await db.commit()
        with pytest.raises(HTTPException) as denied:
            await _enqueue(db, seed, 'company-conditions', attachments=attachments)
        assert denied.value.status_code in {403, 413}


async def test_new_live_approval_is_exact_once_and_consumed_by_actual_owner_api(approval_api, app_user_sessionmaker):
    seed, api = approval_api
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        await _allow_without_approval(db, seed)
        queued = await _enqueue(db, seed, 'live-approval')
        policy = await _policy(db, seed)
        policy.requires_approval = True
        await db.commit()
        assert await channel.poll_pending_channel_messages(db, context=seed['selected'], message_id=UUID(queued['id'])) == []
        await db.refresh(policy)
        message = await db.get(LocalAgentChannelMessage, UUID(queued['id']), populate_existing=True)
        assert message.status == 'waiting_approval' and message.approval_id != seed['approval_id']
        assert message.delivery_attempt_count == 0
        approval_id = message.approval_id
        assert await channel.poll_pending_channel_messages(db, context=seed['selected'], message_id=message.id) == []
        approvals = list(await db.scalars(select(ApprovalRequest).where(ApprovalRequest.agent_id == seed['agent_id'])))
        assert len(approvals) == 2
    response = await api.post(f"/agents/{seed['agent_id']}/approvals/{approval_id}/resolve", json={'action': 'approve'})
    assert response.status_code == 200, response.text
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        leased, = await channel.poll_pending_channel_messages(db, context=seed['selected'], message_id=UUID(queued['id']))
        assert leased['id'] == queued['id'] and leased['delivery_attempt_count'] == 1
        assert await channel.poll_pending_channel_messages(db, context=seed['selected'], message_id=UUID(queued['id'])) == []


@pytest.mark.parametrize('condition', [{'allowed_sources': ['web']}, {'max_content_chars': 4}])
async def test_current_action_conditions_stop_queued_work_and_return_actual_reason(
    delegation, app_user_sessionmaker, condition,
):
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await _allow_without_approval(db, seed)
        policy = await _policy(db, seed)
        policy.conditions = condition
        await db.commit()
        assert await channel.poll_pending_channel_messages(db, context=seed['selected'], message_id=message.id) == []
        await db.refresh(message)
        assert message.status == 'rejected' and message.delivery_attempt_count == 0
        approval = await db.get(ApprovalRequest, message.approval_id)
        assert approval.status == 'approved' and approval.execution_status == 'failed'
        status = await channel.get_a2a_channel_message_status(db, message_id=message.id,
            principal=seed['principal'], source_agent_id=seed['source'])
        assert status['execution_state'] == 'not_dispatched' and status['next_action'] == 'revise_original_work'
        notice, = list(await db.scalars(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(message.id))))
        assert notice.parent_session_id == seed['parent'] and notice.terminal_status == 'rejected'
        assert message.result
        event = await channel.record_channel_event(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=None, event_type='text', payload={'text': 'Independent analysis is still authorized.'})
        assert event['payload']['text'] == 'Independent analysis is still authorized.'


@pytest.mark.parametrize('ambiguous', ['legacy', 'expired_delivery'])
async def test_policy_tightening_never_replays_ambiguous_work_as_unexecuted(
    delegation, app_user_sessionmaker, ambiguous,
):
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await _allow_without_approval(db, seed)
        if ambiguous == 'expired_delivery':
            assert await channel.poll_pending_channel_messages(db, context=seed['selected'], message_id=message.id)
            message.delivery_lease_expires_at = channel.utcnow() - timedelta(seconds=1)
        else:
            origin = await db.scalar(select(InvocationSpan).where(InvocationSpan.request_id == message.id,
                InvocationSpan.span_type == 'remote_action'))
            origin.metadata_json = {k: v for k, v in origin.metadata_json.items() if k != 'dispatch_protocol'}
        policy = await _policy(db, seed)
        policy.conditions = {'max_content_chars': 4}
        await db.commit()
        assert await channel.poll_pending_channel_messages(db, context=seed['selected'], message_id=message.id) == []
        await db.refresh(message)
        assert message.status == 'needs_reconciliation'
        status = await channel.get_a2a_channel_message_status(db, message_id=message.id,
            principal=seed['principal'], source_agent_id=seed['source'])
        assert status['execution_state'] == 'unknown' and status['next_action'] == 'inspect_original_runner'
        approval = await db.get(ApprovalRequest, message.approval_id)
        assert approval.status == 'approved' and approval.execution_status == 'needs_reconciliation'
        report = await channel.record_channel_result(db, context=seed['selected'], session_id=seed['work_id'],
            message_id=message.id, result_status='completed', output='The actual late result remains available.')
        assert report['message']['result'] == 'The actual late result remains available.'


async def test_cached_policy_and_message_cannot_override_fresh_committed_state(delegation, app_user_sessionmaker):
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as stale:
        message = await _allow_without_approval(stale, seed)
        policy = await _policy(stale, seed)
        assert message.status == 'pending' and policy.conditions == {'test': True}
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as fresh:
            current = await _policy(fresh, seed)
            current.conditions = {'max_content_chars': 4}
            await fresh.commit()
        assert await channel.poll_pending_channel_messages(stale, context=seed['selected'], message_id=message.id) == []
        await stale.refresh(message)
        assert message.status == 'rejected' and message.delivery_attempt_count == 0


async def test_concurrent_polls_create_only_one_live_approval(delegation, app_user_sessionmaker):
    import asyncio
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        await _allow_without_approval(db, seed)
        queued = await _enqueue(db, seed, 'concurrent-live-approval')
        policy = await _policy(db, seed)
        policy.requires_approval = True
        await db.commit()
    async def poll():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            return await channel.poll_pending_channel_messages(db, context=seed['selected'], message_id=UUID(queued['id']))
    assert await asyncio.gather(poll(), poll()) == [[], []]
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        approvals = list(await db.scalars(select(ApprovalRequest).where(ApprovalRequest.agent_id == seed['agent_id'])))
        assert len(approvals) == 2
        projected = await channel.list_channel_requests(db, tenant_id=seed['tenant_id'], session_id=seed['work_id'],
            owner_user_id=seed['user_id'], actor_user_id=seed['user_id'])
        request, = [r for r in projected['requests'] if r['id'] == queued['id']]
        assert request['status'] == 'waiting_approval' and request['approval_id']
        assert request['target_agent_id'] == str(seed['agent_id']) and request['execution_state'] == 'not_dispatched'


async def test_direct_websocket_dispatch_obeys_current_action_conditions(delegation, app_user_sessionmaker, monkeypatch):
    from app.api import local_agent_channel as api
    seed = delegation
    class Socket:
        def __init__(self):
            self.frames = []
        async def send_json(self, payload):
            self.frames.append(payload)
    socket = Socket()
    manager = api.LocalAgentChannelConnectionManager()
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await _allow_without_approval(db, seed)
        policy = await _policy(db, seed)
        policy.conditions = {'max_content_chars': 4}
        await db.commit()
        await manager.connect(context=seed['selected'], websocket=socket)
        monkeypatch.setattr(api, 'channel_ws_manager', manager)
        result = await channel.dispatch_channel_message(db, message_id=message.id, owner_user_id=seed['user_id'])
        assert result['status'] == 'not_pending' and not socket.frames
        await db.refresh(message)
        assert message.status == 'rejected' and message.delivery_attempt_count == 0


async def test_original_work_and_existing_approval_component_use_real_jwt_state(
    delegation, app_user_sessionmaker, tmp_path,
):
    from fastapi import FastAPI
    from app.api import agents, local_agent_channel
    from app.core.security import create_access_token
    from app.database import get_db
    from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge
    seed = delegation
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        await _allow_without_approval(db, seed)
        queued = await _enqueue(db, seed, 'mounted-live-approval')
        policy = await _policy(db, seed)
        policy.requires_approval = True
        await db.commit()
        assert await channel.poll_pending_channel_messages(db, context=seed['selected'], message_id=UUID(queued['id'])) == []
        owner = await db.get(User, seed['user_id'])
        token = create_access_token(str(owner.id), owner.role, str(owner.tenant_id))
    web = FastAPI()
    web.include_router(agents.router, prefix='/api')
    web.include_router(local_agent_channel.router, prefix='/api')
    async def database():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            yield db
    web.dependency_overrides[get_db] = database
    await _exercise_dom_bridge(web, seed, tmp_path, entry_file='e2e/local-dispatch-approval-native.tsx',
        extra_env={'HIVE_M3F_SESSION_ID': str(seed['work_id']), 'HIVE_M3F_TOKEN': token})
    async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, UUID(queued['id']))
        approval = await db.get(ApprovalRequest, message.approval_id)
        assert message.status == 'pending' and message.delivery_attempt_count == 0
        assert approval.status == 'approved' and approval.resolved_by == seed['user_id']
        leased, = await channel.poll_pending_channel_messages(db, context=seed['selected'], message_id=message.id)
        assert leased['delivery_attempt_count'] == 1
