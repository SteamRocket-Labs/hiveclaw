"""Ordinary owner chat reaches the selected remote executor and original work."""

import hashlib
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionTurnInput
from app.services.runtime_task_claim_service import RuntimeTaskClaimService
from tests.integration import test_m3o_remote_a2a_tasks_postgres as original
from tests.integration.test_m3q_remote_runtime_postgres import selection
from tests.services.test_remote_a2a_transport import remote_server

app_user_engine = original.app_user_engine
app_user_sessionmaker = original.app_user_sessionmaker
machine_api = original.machine_api
migrated_pg_url = original.migrated_pg_url
owner_engine = original.owner_engine
owner_sessionmaker = original.owner_sessionmaker
pg_container = original.pg_container
remote_work = original.remote_work
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def context_record(parts):
    assert len(parts) == 2 and all('text' in part for part in parts)
    part = parts[1]
    assert part['metadata']['hiveContextSchema'] == 'hive.remote_chat_context.v1'
    assert hashlib.sha256(part['text'].encode()).hexdigest() == part['metadata']['hiveContextSha256']
    return json.loads(part['text'])


async def execute_chat(seed, run_id, factory):
    import asyncio
    from app.services import runtime_task_worker

    async with tenant_scoped_session(seed['tenant'], session_factory=factory) as db:
        tasks = await RuntimeTaskClaimService(db=db, worker_id='remote-chat-worker', task_types=('web_chat_turn',)).claim_available()
        task = next(task for task in tasks if task.id == UUID(run_id))
    assert runtime_task_worker._dispatch_claimed_task(task)
    _kind, process = runtime_task_worker._DISPATCHED_TASKS[task.id.hex]
    await asyncio.wait_for(process, 30)


async def prepare_employee(seed, factory, monkeypatch, tmp_path, *, personality='ORIGINAL_REMOTE_EMPLOYEE_CONTEXT'):
    from app.config import get_settings
    from app.services import agent_context, agent_manager, memory_service, web_chat_runtime
    from app.services.agent_manager import AgentManager

    settings = get_settings()
    monkeypatch.setattr(settings, 'AGENT_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(agent_manager, 'settings', settings)
    monkeypatch.setattr(agent_context, 'PERSISTENT_DATA', tmp_path)
    monkeypatch.setattr(agent_context, 'TOOL_WORKSPACE', tmp_path)
    async with tenant_scoped_session(seed['tenant'], session_factory=factory) as db:
        agent = await db.get(Agent, seed['agent'])
        agent.agent_type = 'native'
        await AgentManager().initialize_agent_files(db, agent, personality=personality,
            original_author_id=seed['owner'])
        await db.commit()
    async def local_memory(*_args, **_kwargs):
        return memory_service.MemoryContextResult(content='', status='ready', code='memory_context_ready',
            user_message='Controlled local memory context', source_refs=(), source_unbound=())
    monkeypatch.setattr(memory_service, 'build_memory_context', local_memory)
    async def no_native(*_args, **_kwargs):
        raise AssertionError('A selected remote employee must not invoke a saved native model')
    monkeypatch.setattr(web_chat_runtime, 'invoke_agent', no_native)


async def settle_boundary(seed, factory, tmp_path, run_id):
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    processor = WebTerminalBoundaryProcessor(session_factory=factory, data_root=tmp_path)
    failures = []
    async def validate(db, item):
        try:
            return await processor.validate(db, item)
        except Exception as exc:
            failures.append(repr(exc))
            raise
    async def consume(item):
        try:
            return await processor(item)
        except Exception as exc:
            failures.append(repr(exc))
            raise
    terminal = await RuntimeTerminalBoundaryOutboxService(session_factory=factory).drain_once(
        tenant_id=seed['tenant'], worker_id='remote-chat-terminal', canonical_validator=validate,
        process_callback=consume, task_types=('web_chat_turn',))
    if terminal['delivered'] != 1:
        from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
        async with tenant_scoped_session(seed['tenant'], session_factory=factory) as db:
            error = await db.scalar(select(RuntimeTerminalBoundaryOutbox.last_error).where(
                RuntimeTerminalBoundaryOutbox.runtime_task_id == UUID(run_id)))
        pytest.fail(f'{terminal}: {error}: {failures}')


@pytest.mark.parametrize('flow', ['completed', 'second_turn', 'unknown', 'input_required', 'cancel', 'runtime_changed', 'source_changed', 'saved_history_changed', 'cancel_inflight'])
async def test_ordinary_chat_uses_canonical_inputs_full_context_and_real_remote_return(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path, flow
):
    from app.api import chat_sessions
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError

    seed, api, session_id = remote_work
    api._transport.app.include_router(chat_sessions.router)
    await prepare_employee(seed, app_user_sessionmaker, monkeypatch, tmp_path)
    from app.services import memory_service
    memory_queries = []
    local_memory = memory_service.build_memory_context
    async def capture_memory(*args, **kwargs):
        memory_queries.append(kwargs['query'])
        return await local_memory(*args, **kwargs)
    monkeypatch.setattr(memory_service, 'build_memory_context', capture_memory)
    with remote_server() as remote:
        path = await original.configure(seed, api, remote)
        selected = await api.post(path + '/runtime', json=selection(seed))
        assert selected.status_code == 200, selected.text
        text = 'COMPLETE_REMOTE_CHAT_RESPONSE\n' * 1500
        remote.reply = lambda request: {'task': {'id': 'remote-chat-' + ('1' if flow in {'input_required', 'cancel', 'cancel_inflight'} else str(sum(r[0] == 'POST' for r in remote.requests))),
            'contextId': 'durable-remote-chat-context', 'status': {'state': ('TASK_STATE_INPUT_REQUIRED' if flow in {'input_required', 'cancel', 'cancel_inflight'} else 'TASK_STATE_COMPLETED'), 'message': {
                'messageId': 'reply-' + request['id'], 'role': 'ROLE_AGENT', 'parts': [{'text': text}]}},
            'artifacts': [{'artifactId': 'report', 'parts': [{'filename': 'report.txt', 'text': text}]}]}}
        if flow == 'unknown':
            rpc = RemoteA2AEndpoint.rpc
            async def lost_reply(endpoint, *args, **kwargs):
                await rpc(endpoint, *args, **kwargs)
                raise RemoteA2AError('remote_a2a_response_lost', execution_status='unknown')
            monkeypatch.setattr(RemoteA2AEndpoint, 'rpc', lost_reply)
        base = f"/agents/{seed['agent']}/sessions/{session_id}"
        body = {'content': 'Prepare the original work with all authorized context.', 'input_id': str(uuid4()),
            'idempotency_key': 'ordinary-remote-first-input'}
        response = await api.post(base + '/runs', json=body)
        assert response.status_code == 201 and response.json().get('run_id'), response.text
        run_id = response.json()['run_id']
        assert (await api.post(base + '/runs', json=body)).json()['run_id'] == run_id
        if flow == 'runtime_changed':
            restored = await api.post(path + '/runtime', json=selection(seed, selected_runtime='previous',
                expected_runtime_revision=1))
            assert restored.status_code == 200, restored.text
        if flow == 'source_changed':
            (tmp_path / str(seed['agent']) / 'soul.md').write_text('Unwitnessed replacement context')
        if flow == 'cancel_inflight':
            rpc = RemoteA2AEndpoint.rpc
            async def cancel_during_send(endpoint, *args, **kwargs):
                result = await rpc(endpoint, *args, **kwargs)
                if args[0] == 'SendMessage':
                    stopped = await api.post(base + f'/runs/{run_id}/cancel', json={})
                    assert stopped.status_code == 200, stopped.text
                return result
            monkeypatch.setattr(RemoteA2AEndpoint, 'rpc', cancel_during_send)
        await execute_chat(seed, run_id, app_user_sessionmaker)
        if flow in {'runtime_changed', 'source_changed'}:
            assert not [row for row in remote.requests if row[0] == 'POST']
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                task = await db.get(RuntimeTask, UUID(run_id))
                assert task.status == 'needs_reconciliation' and task.metadata_json['remote_a2a']['operations'][-1]['phase'] == 'not_started'
                assert not await db.scalar(select(ChatTranscriptEvent.id).where(ChatTranscriptEvent.run_id == task.id,
                    ChatTranscriptEvent.event_type == 'assistant_message'))
            return
        posts = [row[3] for row in remote.requests if row[0] == 'POST']
        assert len(posts) == 1 and posts[0]['method'] == 'SendMessage'
        parts = posts[0]['params']['message']['parts']
        assert parts[0]['text'] == body['content']
        context = context_record(parts)
        assert context['schema'] == 'hive.remote_chat_context.v1'
        assert context['inputs'][0]['input_id'] == body['input_id']
        assert 'ORIGINAL_REMOTE_EMPLOYEE_CONTEXT' in str(context['resources'])
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, UUID(run_id), populate_existing=True)
            assert task.parent_session_id == task.child_session_id == str(session_id)
            assert task.root_user_id == seed['owner'] and task.budget_run_id is not None
            assert task.status == ('needs_reconciliation' if flow == 'unknown' else 'pending' if flow == 'cancel_inflight' else 'suspended' if flow in {'input_required', 'cancel'} else 'completed'), task.metadata_json
            assert await db.scalar(select(func.count()).select_from(SessionModelResult).where(SessionModelResult.run_id == task.id)) == 0
            row = await db.get(SessionTurnInput, UUID(body['input_id']))
            assert row.status == ('bound' if flow == 'unknown' else 'applied')
        assert (await api.post(base + '/runs', json=body)).json()['run_id'] == run_id
        assert len([row for row in remote.requests if row[0] == 'POST']) == 1
        if flow == 'unknown':
            await settle_boundary(seed, app_user_sessionmaker, tmp_path, run_id)
            restored = await api.post(path + '/runtime', json=selection(seed, selected_runtime='previous',
                expected_runtime_revision=1))
            assert restored.status_code == 200, restored.text
            duplicate = await api.post(base + '/runs', json={'content': 'Do not replay an unknown remote effect.',
                'input_id': str(uuid4()), 'idempotency_key': 'new-input-after-unknown'})
            assert duplicate.status_code == 202 and not duplicate.json().get('run'), duplicate.text
            assert duplicate.json()['dispatch']['code'] == 'remote_a2a_reconciliation_required'
            abandoned = await api.post(path + f'/tasks/{run_id}/operations', json={
                'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant']),
                'expected_version': 1, 'operation_id': str(uuid4()), 'action': 'abandon', 'params': {}})
            assert abandoned.status_code == 200 and abandoned.json()['local_abandoned'], abandoned.text
            next_input = await api.post(base + '/runs', json={'content': 'New work after explicit local abandonment.',
                'input_id': str(uuid4()), 'idempotency_key': 'after-explicit-abandon'})
            assert next_input.status_code == 201 and next_input.json()['run_id'] != run_id, next_input.text
            assert len([row for row in remote.requests if row[0] == 'POST']) == 1
            return
        await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='remote-chat-return')
        events = (await api.get(base + '/transcript')).json()
        assert any(event.get('content') == text for event in events), str(events)[:1500]
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            reply = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.run_id == UUID(run_id), ChatTranscriptEvent.event_type == 'assistant_message'))
            assert reply is not None and reply.content == text
        if flow in {'input_required', 'cancel', 'cancel_inflight'}:
            state = 'TASK_STATE_CANCELED' if flow in {'cancel', 'cancel_inflight'} else 'TASK_STATE_COMPLETED'
            remote.reply = lambda request: ({'id': 'remote-chat-1', 'contextId': 'durable-remote-chat-context',
                'status': {'state': state}} if request['method'] == 'CancelTask' else {'task': {
                'id': 'remote-chat-1', 'contextId': 'durable-remote-chat-context', 'status': {'state': state,
                    'message': {'messageId': 'follow-up-' + request['id'], 'role': 'ROLE_AGENT', 'parts': [{'text': 'Follow-up consumed.'}]}}}})
            if flow == 'cancel':
                cancel = await api.post(base + f'/runs/{run_id}/cancel', json={})
                assert cancel.status_code == 200, cancel.text
            elif flow == 'input_required':
                reply_body = {'content': 'Here is the requested detail.', 'input_id': str(uuid4()),
                    'idempotency_key': 'ordinary-remote-reply'}
                reply = await api.post(base + '/runs', json=reply_body)
                assert reply.status_code == 202, reply.text
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                queued = await db.get(RuntimeTask, UUID(run_id))
                assert queued.status == 'pending', (queued.metadata_json, cancel.text if flow == 'cancel' else reply.text if flow == 'input_required' else queued.status)
            await execute_chat(seed, run_id, app_user_sessionmaker)
            posts = [row[3] for row in remote.requests if row[0] == 'POST']
            assert len(posts) == 2
            assert posts[-1]['method'] == ('CancelTask' if flow in {'cancel', 'cancel_inflight'} else 'SendMessage')
            if flow == 'input_required':
                assert posts[-1]['params']['message']['taskId'] == 'remote-chat-1'
                assert posts[-1]['params']['message']['parts'][0]['text'] == reply_body['content']
                assert memory_queries[-1] == reply_body['content']
                assert posts[-1]['params']['message']['contextId'] == 'durable-remote-chat-context'
                continued = context_record(posts[-1]['params']['message']['parts'])
                assert any(message.get('content') == text for message in continued['messages']), 'The prior remote question must remain in the same work context.'
                assert sum(message.get('content') == body['content'] for message in continued['messages']) == 1
                assert sum(message.get('content') == reply_body['content'] for message in continued['messages']) == 1
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                task = await db.get(RuntimeTask, UUID(run_id))
                assert task.status == ('killed' if flow in {'cancel', 'cancel_inflight'} else 'completed'), task.metadata_json
                if flow == 'input_required':
                    assert (await db.get(SessionTurnInput, UUID(reply_body['input_id']))).status == 'applied'
            return
        if flow in {'second_turn', 'saved_history_changed'}:
            await settle_boundary(seed, app_user_sessionmaker, tmp_path, run_id)
            next_body = {'content': 'Continue the same work without losing the prior result.', 'input_id': str(uuid4()),
                'idempotency_key': 'ordinary-remote-second-input'}
            successor = await api.post(base + '/runs', json=next_body)
            assert successor.status_code == 201 and successor.json().get('run_id'), successor.text
            if flow == 'saved_history_changed':
                async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                    (await db.get(ChatTranscriptEvent, reply.id)).content = 'Unproven replacement history'
                    await db.commit()
            await execute_chat(seed, successor.json()['run_id'], app_user_sessionmaker)
            if flow == 'saved_history_changed':
                assert len([row for row in remote.requests if row[0] == 'POST']) == 1
                async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                    held = await db.get(RuntimeTask, UUID(successor.json()['run_id']))
                    state = held.metadata_json['remote_a2a']
                    assert held.status == 'needs_reconciliation' and state['operations'][-1]['phase'] == 'not_started'
                    assert state['source_error']['code'] == 'remote_chat_history_unavailable'
                return
            posts = [row[3] for row in remote.requests if row[0] == 'POST']
            assert len(posts) == 2 and posts[-1]['params']['message']['contextId'] == 'durable-remote-chat-context'
            context = context_record(posts[-1]['params']['message']['parts'])
            assert any(message.get('content') == text for message in context['messages'])


async def test_ordinary_remote_chat_client_and_original_work_renderer_use_real_http_worker(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path
):
    import asyncio
    from fastapi import FastAPI
    from app.api import agents, chat_sessions
    from app.database import get_db
    from app.services import runtime_task_worker
    from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge

    seed, api, session_id = remote_work
    await prepare_employee(seed, app_user_sessionmaker, monkeypatch, tmp_path)
    web = FastAPI()
    for router in (agents.router, chat_sessions.router):
        web.include_router(router, prefix='/api')
    async def dependency():
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            yield db
    web.dependency_overrides[get_db] = dependency
    queued, finished = asyncio.Queue(), asyncio.Queue()
    failures = []
    async def wake(**kwargs):
        queued.put_nowait(str(kwargs['runtime_task_id']))
    async def consume():
        try:
            run_id = await asyncio.wait_for(queued.get(), 30)
            await execute_chat(seed, run_id, app_user_sessionmaker)
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.put_nowait(True)
    @web.get('/__remote-test/wait')
    async def wait_for_worker():
        await asyncio.wait_for(finished.get(), 20)
        assert not failures, failures
        return {'worker_finished': True}
    monkeypatch.setattr(runtime_task_worker, 'notify_runtime_task_worker', wake)
    with remote_server() as remote:
        path = await original.configure(seed, api, remote)
        assert (await api.post(path + '/runtime', json=selection(seed))).status_code == 200
        remote.reply = lambda request: {'task': {'id': 'native-original-work', 'status': {
            'state': 'TASK_STATE_COMPLETED', 'message': {'messageId': 'reply-' + request['id'],
                'role': 'ROLE_AGENT', 'parts': [{'text': 'COMPLETE_REMOTE_CHAT_RESPONSE\n' * 1500}]}}}}
        worker = asyncio.create_task(consume())
        try:
            await _exercise_dom_bridge(web, {'user_id': seed['owner'], 'tenant_id': seed['tenant'], 'agent_id': seed['agent']},
                tmp_path, entry_file='e2e/remote-a2a-work-native.tsx', extra_env={'HIVE_M3N_TOKEN': seed['user_token'],
                    'HIVE_M3O_SESSION_ID': str(session_id), 'HIVE_M3T_CHAT': '1'})
            await asyncio.wait_for(worker, 5)
            assert not failures
            assert len([request for request in remote.requests if request[0] == 'POST']) == 1
        finally:
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_stopped_budget_does_not_treat_a_non_remote_run_as_recovery(
    remote_work, app_user_sessionmaker
):
    from copy import deepcopy
    from app.api import chat_sessions
    from app.models.runtime_budget import RuntimeBudgetRun

    seed, api, session_id = remote_work
    api._transport.app.include_router(chat_sessions.router)
    with remote_server() as remote:
        path = await original.configure(seed, api, remote)
        assert (await api.post(path + '/runtime', json=selection(seed))).status_code == 200
        response = await api.post(f"/agents/{seed['agent']}/sessions/{session_id}/runs", json={
            'content': 'Keep the stopped budget closed.', 'input_id': str(uuid4()), 'idempotency_key': 'closed-budget'})
        assert response.status_code == 201, response.text
        task_id = UUID(response.json()['run_id'])
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, task_id)
            budget = await db.get(RuntimeBudgetRun, task.budget_run_id)
            budget.status = 'cancelled'
            metadata = deepcopy(task.metadata_json)
            metadata['remote_a2a'].pop('dispatch_kind')
            metadata['remote_a2a']['operations'][-1]['method'] = 'GetTask'
            task.metadata_json = metadata
            await db.commit()
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            claimed = await RuntimeTaskClaimService(db=db, worker_id='closed-budget',
                task_types=('web_chat_turn',)).claim_available()
            assert task_id not in {task.id for task in claimed}
        assert not [request for request in remote.requests if request[0] == 'POST']


async def test_remote_context_credentials_are_checked_before_text_encoding(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path
):
    from app.api import chat_sessions

    seed, api, session_id = remote_work
    api._transport.app.include_router(chat_sessions.router)
    secret = 'synthetic-quote"slash\\credential'
    await prepare_employee(seed, app_user_sessionmaker, monkeypatch, tmp_path,
        personality=f'Authorized background must not disclose this credential: {secret}')
    with remote_server() as remote:
        path = f"/agents/{seed['agent']}/remote-a2a"
        config = original.configuration(seed, remote)
        config['bearer_token'] = secret
        configured = await api.put(path, json=config)
        assert configured.status_code == 200, configured.text
        discovered = await api.post(path + '/discover', json=original.request_frame(seed, 1))
        assert discovered.status_code == 200, discovered.text
        assert (await api.post(path + '/runtime', json=selection(seed))).status_code == 200
        response = await api.post(f"/agents/{seed['agent']}/sessions/{session_id}/runs", json={
            'content': 'Use the authorized background.', 'input_id': str(uuid4()),
            'idempotency_key': 'escaped-context-credential'})
        assert response.status_code == 201, response.text
        run_id = response.json()['run_id']
        await execute_chat(seed, run_id, app_user_sessionmaker)
        assert not [request for request in remote.requests if request[0] == 'POST']
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, UUID(run_id))
            state = task.metadata_json['remote_a2a']
            assert task.status == 'needs_reconciliation' and state['operations'][-1]['phase'] == 'not_started'
            assert state['source_error']['code'] == 'remote_a2a_credential_in_request'
