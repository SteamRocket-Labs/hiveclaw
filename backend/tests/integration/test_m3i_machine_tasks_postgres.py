"""Machine task admission keeps requester identity independent of its human issuer."""
import json
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import func, select

from app.database import tenant_scoped_session
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from tests.integration.test_m3h_machine_identity_postgres import machine_api as _machine_api, _issue
from tests.integration import conftest as _database_fixtures

machine_api = _machine_api

# Global return-worker assertions require an independent catalog.
app_user_engine = _database_fixtures.app_user_engine
app_user_sessionmaker = _database_fixtures.app_user_sessionmaker
migrated_pg_url = _database_fixtures.migrated_pg_url
owner_engine = _database_fixtures.owner_engine
owner_sessionmaker = _database_fixtures.owner_sessionmaker
pg_container = _database_fixtures.pg_container

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


def _send(text='Prepare the partner report.', *, message_id=None):
    return {'jsonrpc': '2.0', 'id': 'send-1', 'method': 'SendMessage', 'params': {
        'message': {'messageId': message_id or str(uuid4()), 'role': 'ROLE_USER', 'parts': [{'text': text}]},
        'configuration': {'returnImmediately': True}}}


async def _dispatch_machine_input(seed, task_id, app_user_sessionmaker):
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.services.session_input_dispatch import recover_admitted_session_inputs_once

    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        ingress = await db.get(ChannelIngressEvent, uuid5(UUID(str(task_id)), 'ingress'))
        row = await db.get(SessionTurnInput, UUID(ingress.metadata_json['input_id']))
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == row.id,
            SessionInputAdmission.input_revision == row.revision))
        await recover_admitted_session_inputs_once(db, worker_id='machine-input-dispatch', tenant_id=seed['tenant'],
            admission_ids=(admission.id,))


async def test_machine_message_enters_real_shared_runtime_once_without_a_user_alias(
    machine_api, app_user_sessionmaker, monkeypatch,
):
    seed, api = machine_api
    async def no_wakeup(**_kwargs):
        pass
    monkeypatch.setattr('app.services.runtime_task_worker.notify_runtime_task_worker', no_wakeup)
    grant = _issue(seed)
    grant['scopes'] = ['agent.describe', 'agent.execute', 'task.read', 'task.cancel']
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json=grant)
    assert issued.status_code == 201, issued.text
    identity = issued.json()
    headers = {'Authorization': 'Bearer ' + identity['token'], 'A2A-Version': '1.0'}
    path = f"/agents/{seed['agent']}/a2a/rpc"
    request = _send()
    response = await api.post(path, json=request, headers=headers)
    assert response.status_code == 200 and 'result' in response.json(), response.text
    task = response.json()['result']['task']
    assert task['status']['state'] == 'TASK_STATE_SUBMITTED'
    assert task['history'] == [{**request['params']['message'], 'taskId': task['id'], 'contextId': task['contextId']}]
    repeated = await api.post(path, json={**request, 'id': 'different-rpc-id'}, headers=headers)
    assert repeated.status_code == 200 and repeated.json()['result']['task']['id'] == task['id']
    changed = _send('An unrelated replacement.', message_id=request['params']['message']['messageId'])
    conflict = await api.post(path, json=changed, headers=headers)
    assert 'error' in conflict.json()
    await _dispatch_machine_input(seed, task['id'], app_user_sessionmaker)
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        run = await db.get(RuntimeTask, UUID(task['id']))
        session = await db.get(ChatSession, UUID(task['contextId']))
        assert run.root_user_id is None and session.user_id is None
        assert run.root_external_principal_id == session.external_principal_id == UUID(identity['id'])
        assert run.machine_credential_id == UUID(identity['credential_id'])
        assert run.parent_agent_id == session.agent_id == seed['agent']
        assert await db.scalar(select(func.count(RuntimeTask.id)).where(RuntimeTask.parent_agent_id == seed['agent'])) == 1
        ingress = await db.scalar(select(ChannelIngressEvent).where(ChannelIngressEvent.agent_id == seed['agent']))
        assert ingress.external_principal_id == run.root_external_principal_id
        assert identity['token'] not in json.dumps([run.metadata_json, ingress.payload_json], default=str)
    read = await api.post(path, json={'jsonrpc': '2.0', 'id': 2, 'method': 'GetTask', 'params': {'id': task['id']}}, headers=headers)
    assert read.status_code == 200 and read.json()['result']['id'] == task['id']


async def test_description_grant_does_not_silently_acquire_task_authority(machine_api):
    seed, api = machine_api
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json=_issue(seed))
    assert issued.status_code == 201
    description = await api.get(f"/agents/{seed['agent']}/machine-description", headers={
        'Authorization': 'Bearer ' + issued.json()['token']})
    transport = description.json()['interoperability']['a2a_tasks']
    assert transport['status'] == 'available' and transport['methods'] == {}
    assert transport['history'] == 'logical_task'
    assert transport['streaming'] is False and transport['push_notifications'] is False
    denied = await api.post(f"/agents/{seed['agent']}/a2a/rpc", json=_send(), headers={
        'Authorization': 'Bearer ' + issued.json()['token'], 'A2A-Version': '1.0'})
    assert denied.status_code == 403


async def test_machine_description_methods_consume_the_advertised_route_under_exact_scopes(machine_api):
    seed, api = machine_api
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed),
        'scopes': ['agent.describe', 'agent.execute', 'task.read', 'task.cancel']})
    assert issued.status_code == 201, issued.text
    headers = {'Authorization': 'Bearer ' + issued.json()['token'], 'A2A-Version': '1.0'}
    description = await api.get(f"/agents/{seed['agent']}/machine-description", headers=headers)
    transport = description.json()['interoperability']['a2a_tasks']
    assert transport['protocol_version'] == '1.0' and transport['transport'] == 'JSONRPC'
    assert transport['methods'] == {'SendMessage': 'agent.execute', 'GetTask': 'task.read', 'CancelTask': 'task.cancel'}
    started = await api.post(transport['rpc_url'], json=_send(), headers=headers)
    assert started.status_code == 200 and 'result' in started.json(), started.text
    task_id = started.json()['result']['task']['id']
    read = await api.post(transport['rpc_url'], json={'jsonrpc': '2.0', 'id': 'description-read',
        'method': 'GetTask', 'params': {'id': task_id}}, headers=headers)
    assert read.json()['result']['id'] == task_id
    cancelled = await api.post(transport['rpc_url'], json={'jsonrpc': '2.0', 'id': 'description-cancel',
        'method': 'CancelTask', 'params': {'id': task_id}}, headers=headers)
    assert cancelled.json()['result']['status']['state'] == 'TASK_STATE_SUBMITTED', cancelled.text
    assert cancelled.json()['result']['metadata']['hive']['cancellationStatus'] == 'applying'


async def test_machine_history_and_receipts_preserve_exact_ingress_secret_protection(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.services.secrets_provider import get_secrets_provider

    seed, api = machine_api
    await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed),
        'scopes': ['agent.execute', 'task.read']})
    identity = issued.json()
    bearer = identity['token']
    model_secret = 'synthetic-machine-only'
    headers = {'Authorization': 'Bearer ' + bearer, 'A2A-Version': '1.0'}
    request = _send(f'Keep {model_secret} and {bearer} out of the prompt. Preserve api_key=sk-example-not-authority.')
    request['params']['metadata'] = {bearer: model_secret}
    request['params']['message']['parts'][0]['metadata'] = {'private': model_secret}
    path = f"/agents/{seed['agent']}/a2a/rpc"
    response = await api.post(path, json=request, headers=headers)
    assert response.status_code == 200 and 'result' in response.json(), response.text
    task = response.json()['result']['task']
    text = task['history'][0]['parts'][0]['text']
    assert model_secret not in text and bearer not in text
    assert 'api_key=sk-example-not-authority' in text and text.count('[REDACTED_SECRET]') == 2
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        run = await db.get(RuntimeTask, UUID(task['id']))
        ingress = await db.get(ChannelIngressEvent, uuid5(UUID(task['id']), 'ingress'))
        row = await db.get(SessionTurnInput, uuid5(UUID(identity['id']), 'a2a-message:' + request['params']['message']['messageId']))
        command = await db.get(SessionCommand, row.command_id)
        stored = json.dumps([run.metadata_json, ingress.payload_json, command.target_json, row.content_parts_json])
        assert model_secret not in stored and bearer not in stored
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed['agent'])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt('synthetic-rotated-machine-only')
        await db.commit()
    replayed = await api.post(path, json=request, headers=headers)
    assert replayed.json()['result']['task']['id'] == task['id'], replayed.text
    assert replayed.json()['result']['task']['history'] == task['history']
    invalid_id = await api.post(path, json=_send('A separate request.', message_id=bearer), headers=headers)
    assert invalid_id.json().get('error', {}).get('code') == -32602, invalid_id.text
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        assert await db.scalar(select(func.count(RuntimeTask.id)).where(RuntimeTask.parent_agent_id == seed['agent'])) == 1


async def _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    import app.database as database
    from app.config import get_settings
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.llm import LLMModel
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.secrets_provider import get_secrets_provider

    root = tmp_path / 'machine-agents'
    monkeypatch.setattr(get_settings(), 'AGENT_DATA_DIR', str(root))
    monkeypatch.setattr(agent_manager, 'settings', get_settings())
    monkeypatch.setattr(agent_context, 'PERSISTENT_DATA', root)
    monkeypatch.setattr(agent_context, 'TOOL_WORKSPACE', root)
    monkeypatch.setattr(agent_tools, 'WORKSPACE_ROOT', root)
    monkeypatch.setattr('app.tools.workspace.WORKSPACE_ROOT', root)
    monkeypatch.setattr(agent_tools, '_TOOL_RUNTIME_SERVICE', None)
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed['agent'])
        model = LLMModel(id=uuid4(), tenant_id=seed['tenant'], provider='openai', model='machine-native-model',
            label='Machine native model', enabled=True, max_input_tokens=256_000,
            base_url='http://127.0.0.1:1', api_key_encrypted=get_secrets_provider().encrypt('synthetic-machine-only'))
        db.add(model)
        await db.flush()
        agent.primary_model_id = model.id
        agent.default_session_permission_mode = 'bypassPermissions'
        db.add(CapabilityPolicy(tenant_id=seed['tenant'], capability='workspace.file.write', allowed=True, requires_approval=False))
        await AgentManager().initialize_agent_files(db, agent, personality='MACHINE_EMPLOYEE_ROLE', original_author_id=seed['owner'])
        await db.commit()
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, '__name__', '').startswith('app.'):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    async def no_wakeup(**_kwargs):
        pass
    monkeypatch.setattr('app.services.runtime_task_worker.notify_runtime_task_worker', no_wakeup)
    class AuxiliaryTransport:
        async def stream(self, **_kwargs):
            return SimpleNamespace(content=json.dumps({'summary': 'Synthetic machine summary.'}))
        async def close(self):
            pass
    monkeypatch.setattr('app.services.llm_client.create_llm_client_from_config', lambda *_args: AuxiliaryTransport())
    return root / str(seed['agent'])


async def _run_native_machine_task(seed, task_id, app_user_sessionmaker):
    from datetime import datetime, timedelta, timezone
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id)
        task.status, task.claimed_by, task.claim_version, task.attempt_count = 'running', 'machine-approval-worker', 1, 1
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.commit()
    fence = set_runtime_task_fence(task_id=task_id, worker_id='machine-approval-worker', claim_version=1)
    try:
        await execute_web_chat_run(task_id)
    finally:
        reset_runtime_task_fence(fence)


async def test_machine_artifact_consumer_reads_complete_pinned_file_with_current_authority(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import hashlib
    import app.runtime.invoker as invoker
    from app.models.chat_artifact import ChatArtifact
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, api = machine_api
    root = await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed),
        'scopes': ['agent.execute', 'task.read']})
    assert issued.status_code == 201, issued.text
    identity = issued.json()
    headers = {'Authorization': 'Bearer ' + identity['token'], 'A2A-Version': '1.0'}
    rpc = f"/agents/{seed['agent']}/a2a/rpc"
    started = await api.post(rpc, json=_send('Create and deliver the complete partner report as an HTML file.'), headers=headers)
    task = started.json()['result']['task']
    task_id = UUID(task['id'])
    content = '<!doctype html><html lang="en"><title>Partner report</title><body>' + ('<p>完整交付 — Partner report.</p>' * 3500) + '</body></html>'
    raw = content.encode()
    assert len(raw) > 64 * 1024
    model = ScriptedLLMClient([{'tool_calls': [{'id': 'machine-deliver-file', 'type': 'function', 'function': {
        'name': 'write_file', 'arguments': json.dumps({'path': 'workspace/partner-report.html', 'content': content})}}],
        'finish_reason': 'tool_calls'}, {'content': '[Partner report](workspace/partner-report.html)', 'finish_reason': 'stop'}])
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: model)
    await _run_native_machine_task(seed, task_id, app_user_sessionmaker)
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        files = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == task_id)))
        assert len(files) == 1 and files[0].authority_state == 'owned'
        artifact = files[0]
        assert artifact.owner_user_id is None and artifact.root_session_id == UUID(task['contextId'])
        assert artifact.snapshot_json['content_hash'] == hashlib.sha256(raw).hexdigest()
    current = await api.post(rpc, json={'jsonrpc': '2.0', 'id': 'files', 'method': 'GetTask',
        'params': {'id': task['id']}}, headers=headers)
    result = current.json()['result']
    assert result['status']['state'] == 'TASK_STATE_COMPLETED', current.text
    delivered = [row for row in result['artifacts'] if row['artifactId'] == str(artifact.id)]
    assert len(delivered) == 1
    part = delivered[0]['parts'][0]
    assert part['filename'] == 'partner-report.html' and part['mediaType'] == 'text/html'
    download = await api.get(part['url'], headers=headers)
    assert download.status_code == 200 and download.content == raw, download.text[:300]
    assert download.headers['cache-control'] == 'private, no-store'
    (root / 'workspace/partner-report.html').write_text('A later workspace version.')
    assert (await api.get(part['url'], headers=headers)).content == raw
    other = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed), 'scopes': ['task.read']})
    foreign = await api.get(part['url'], headers={**headers, 'Authorization': 'Bearer ' + other.json()['token']})
    assert foreign.status_code == 404 and raw not in foreign.content
    second = _send('A separate work item in the same context.')
    second['params']['message']['contextId'] = task['contextId']
    separate = await api.post(rpc, json=second, headers=headers)
    other_task_id = separate.json()['result']['task']['id']
    wrong_work = await api.get(part['url'].replace(task['id'], other_task_id), headers=headers)
    assert wrong_work.status_code == 404
    from app.models.machine_credential import MachineCredential

    async with owner_sessionmaker() as db:
        credential = await db.get(MachineCredential, UUID(identity['credential_id']))
        credential.scopes = ['task.read']
        await db.commit()
    assert (await api.get(part['url'], headers=headers)).content == raw
    assert (await api.post(rpc, json=_send('A new effect after execution scope was removed.'), headers=headers)).status_code == 403
    captured = root / artifact.snapshot_json['snapshot_storage_path']
    captured.write_bytes(b'FORGED_SNAPSHOT')
    changed = await api.get(part['url'], headers=headers)
    assert changed.status_code == 409 and b'FORGED_SNAPSHOT' not in changed.content
    captured.write_bytes(raw)
    captured.unlink()
    missing = await api.get(part['url'], headers=headers)
    assert missing.status_code == 404 and b'A later workspace version.' not in missing.content
    revoked = await api.post(f"/agents/{seed['agent']}/machine-identities/{identity['id']}/revoke", json={
        'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant'])})
    assert revoked.status_code == 200
    assert (await api.get(part['url'], headers=headers)).status_code == 401
    assert len(model.requests) == 2
    (tmp_path / 'machine-file-response.json').write_text(current.text)


async def test_machine_can_deliver_a_consumed_file_from_an_earlier_work_in_its_context(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, drain_terminal_boundary_for_task,
):
    import app.runtime.invoker as invoker
    from app.models.chat_artifact import ChatArtifact
    from app.models.machine_credential import MachineCredential
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, api = machine_api
    await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed),
        'scopes': ['agent.execute', 'task.read']})
    assert issued.status_code == 201, issued.text
    identity = issued.json()
    headers = {'Authorization': 'Bearer ' + identity['token'], 'A2A-Version': '1.0'}
    rpc = f"/agents/{seed['agent']}/a2a/rpc"
    content = 'Partner source with its original provenance.\n完整来源。'
    model = ScriptedLLMClient([
        {'tool_calls': [{'id': 'save-machine-source', 'type': 'function', 'function': {'name': 'write_file',
            'arguments': json.dumps({'path': 'workspace/source.txt', 'content': content})}}], 'finish_reason': 'tool_calls'},
        {'content': 'The source is saved for the next work item.', 'finish_reason': 'stop'},
        {'tool_calls': [{'id': 'consume-machine-source', 'type': 'function', 'function': {'name': 'read_file',
            'arguments': json.dumps({'path': 'workspace/source.txt'})}}], 'finish_reason': 'tool_calls'},
        {'content': 'DELIVERABLE: workspace/source.txt\n[Consumed source](workspace/source.txt)', 'finish_reason': 'stop'}])
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: model)
    first_request = _send('Save the partner source for another work item.')
    first = await api.post(rpc, json=first_request, headers=headers)
    prior = first.json()['result']['task']
    await _run_native_machine_task(seed, UUID(prior['id']), app_user_sessionmaker)
    from app.services.runtime_task_worker import drain_web_terminal_boundary_outbox_once

    await drain_terminal_boundary_for_task(drain_web_terminal_boundary_outbox_once, task_id=UUID(prior['id']),
        worker_id='machine-file-origin-boundary')
    request = _send('Read and deliver workspace/source.txt without rewriting it.')
    request['params']['message']['contextId'] = prior['contextId']
    second = await api.post(rpc, json=request, headers=headers)
    task = second.json()['result']['task']
    assert task['id'] != prior['id'] and task['contextId'] == prior['contextId']
    await _dispatch_machine_input(seed, task['id'], app_user_sessionmaker)
    await _run_native_machine_task(seed, UUID(task['id']), app_user_sessionmaker)
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        rows = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == UUID(task['id']))))
        assert len(rows) == 1 and rows[0].source == 'workspace_read'
        assert rows[0].snapshot_json['selection_model_result_id']
        assert rows[0].owner_user_id is None and rows[0].authority_state == 'owned'
    async with owner_sessionmaker() as db:
        credential = await db.get(MachineCredential, UUID(identity['credential_id']))
        credential.scopes = ['task.read']
        await db.commit()
    current = await api.post(rpc, json={'jsonrpc': '2.0', 'id': 'read-file', 'method': 'GetTask',
        'params': {'id': task['id']}}, headers=headers)
    assert current.json()['result']['status']['state'] == 'TASK_STATE_COMPLETED', current.text
    history = current.json()['result']['history']
    assert [row['role'] for row in history] == ['ROLE_USER', 'ROLE_AGENT']
    assert history[0]['messageId'] == request['params']['message']['messageId']
    assert first_request['params']['message']['messageId'] not in {row['messageId'] for row in history}
    assert 'The source is saved for the next work item.' not in json.dumps(history)
    assert 'Partner source with its original provenance.' not in json.dumps(history, ensure_ascii=False)
    artifact = next(row for row in current.json()['result']['artifacts'] if row['artifactId'] == str(rows[0].id))
    download = await api.get(artifact['parts'][0]['url'], headers=headers)
    assert download.status_code == 200 and download.content == content.encode(), download.text
    assert len(model.requests) == 4


@pytest.mark.parametrize('cancel_successor', [False, True])
async def test_machine_question_returns_to_caller_and_blocking_answer_keeps_task_identity(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, drain_terminal_boundary_for_task, cancel_successor,
):
    import asyncio
    import app.runtime.invoker as invoker
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.services.runtime_task_worker import drain_web_terminal_boundary_outbox_once
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, api = machine_api
    await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed),
        'scopes': ['agent.execute', 'task.read', 'task.cancel']})
    assert issued.status_code == 201, issued.text
    identity = issued.json()
    headers = {'Authorization': 'Bearer ' + identity['token'], 'A2A-Version': '1.0'}
    path = f"/agents/{seed['agent']}/a2a/rpc"
    body = _send('Prepare the partner launch report after confirming its language.')
    response = await api.post(path, json=body, headers=headers)
    original = response.json()['result']['task']
    root_id = UUID(original['id'])
    questions = [{'question': 'Which language should the partner launch report use?', 'header': 'Language',
        'options': [{'label': 'English', 'description': 'For the external partner.'},
            {'label': 'Chinese', 'description': 'For the internal team.'}], 'multiSelect': False}]
    final = 'PARTNER_REPORT_IN_ENGLISH\n' + ('Complete launch details.\n' * 700)
    script = [{'tool_calls': [{'id': 'machine-language-question', 'type': 'function',
        'function': {'name': 'ask_user_question', 'arguments': json.dumps({'questions': questions})}}],
        'finish_reason': 'tool_calls'}]
    if cancel_successor:
        script.append({'tool_calls': [{'id': 'cancel-question-successor', 'type': 'function',
            'function': {'name': 'write_file', 'arguments': json.dumps({'path': 'workspace/after-answer-stop.md',
                'content': 'MUST_NOT_BE_WRITTEN'})}}], 'finish_reason': 'tool_calls'})
    script.append({'content': final, 'finish_reason': 'stop'})
    model = ScriptedLLMClient(script)
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: model)
    await _run_native_machine_task(seed, root_id, app_user_sessionmaker)
    paused = await api.post(path, json={'jsonrpc': '2.0', 'id': 'question', 'method': 'GetTask',
        'params': {'id': str(root_id)}}, headers=headers)
    task = paused.json()['result']
    assert task['status']['state'] == 'TASK_STATE_INPUT_REQUIRED', paused.text
    assert task['status']['message']['role'] == 'ROLE_AGENT'
    card = next(part['data'] for part in task['status']['message']['parts'] if 'data' in part)
    assert card['questions'] == questions and UUID(card['requestItemId'])
    assert task['history'] == [
        {**body['params']['message'], 'taskId': original['id'], 'contextId': original['contextId']},
        task['status']['message'],
    ]
    separate = _send('Prepare a separate work item in this conversation.')
    separate['params']['message']['contextId'] = original['contextId']
    new_work = await api.post(path, json=separate, headers=headers)
    assert 'result' in new_work.json(), new_work.text
    separate_task = new_work.json()['result']['task']
    assert separate_task['id'] != original['id'] and separate_task['contextId'] == original['contextId']
    stopped_separate = await api.post(path, json={'jsonrpc': '2.0', 'id': 'cancel-separate', 'method': 'CancelTask',
        'params': {'id': separate_task['id']}}, headers=headers)
    assert stopped_separate.json()['result']['status']['state'] == 'TASK_STATE_CANCELED', stopped_separate.text
    still_waiting = await api.post(path, json={'jsonrpc': '2.0', 'id': 'still-question', 'method': 'GetTask',
        'params': {'id': original['id']}}, headers=headers)
    assert still_waiting.json()['result']['status'] == task['status'], still_waiting.text
    assert still_waiting.json()['result']['history'] == task['history']
    other = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed),
        'scopes': ['agent.execute', 'task.read']})
    foreign = _send('An answer from another principal.')
    foreign['params']['message']['taskId'] = original['id']
    denied = await api.post(path, json=foreign, headers={**headers, 'Authorization': 'Bearer ' + other.json()['token']})
    assert denied.json()['error']['code'] == -32001, denied.text
    await drain_terminal_boundary_for_task(drain_web_terminal_boundary_outbox_once, task_id=root_id,
        worker_id='machine-question-boundary')
    answer = _send('Use English for the external partner.')
    answer['params']['message']['taskId'] = str(root_id)  # Context is inferred from the existing task.
    conflict = _send('Use English.')
    conflict['params']['message'].update(taskId=str(root_id), contextId=str(uuid4()))
    rejected = await api.post(path, json=conflict, headers=headers)
    assert rejected.json()['error']['code'] == -32602, rejected.text
    answer['params'].pop('configuration')
    notified, notification = asyncio.Event(), {}
    async def wake(**kwargs):
        notification.update(kwargs)
        notified.set()
    monkeypatch.setattr('app.services.runtime_task_worker.notify_runtime_task_worker', wake)
    worker = None
    release = asyncio.Event()
    request = asyncio.create_task(api.post(path, json=answer, headers=headers))
    waiting = asyncio.create_task(notified.wait())
    try:
        await asyncio.wait((request, waiting), timeout=15, return_when=asyncio.FIRST_COMPLETED)
        assert not request.done(), (await request).text
        assert notified.is_set()
        successor_id = UUID(str(notification['runtime_task_id']))
        assert successor_id != root_id
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            successor = await db.get(RuntimeTask, successor_id)
            root = await db.get(RuntimeTask, root_id)
            assert successor.root_runtime_task_id == root_id and successor.budget_run_id == root.budget_run_id
            assert successor.root_user_id is None and successor.root_external_principal_id == UUID(identity['id'])
            row = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.request_item_id == UUID(card['requestItemId'])))
            assert row.intent == 'answer_request'
            command = await db.get(SessionCommand, row.command_id)
            assert command.principal_type == 'external_principal' and command.principal_id == UUID(identity['id'])
        if cancel_successor:
            import app.kernel.engine as engine
            from app.models.session_v2 import SessionControlInput
            from app.config import get_settings
            from pathlib import Path

            entered = asyncio.Event()
            actual = engine._execute_tool_call_with_cancel
            async def hold(*args, **kwargs):
                entered.set()
                await release.wait()
                return await actual(*args, **kwargs)
            monkeypatch.setattr(engine, '_execute_tool_call_with_cancel', hold)
            worker = asyncio.create_task(_run_native_machine_task(seed, successor_id, app_user_sessionmaker))
            await asyncio.wait_for(entered.wait(), 15)
            cancelled = await api.post(path, json={'jsonrpc': '2.0', 'id': 'cancel-successor', 'method': 'CancelTask',
                'params': {'id': original['id']}}, headers=headers)
            assert cancelled.json()['result']['metadata']['hive']['cancellationStatus'] == 'applying', cancelled.text
            release.set()
            await asyncio.wait_for(worker, 15)
            stopped = await asyncio.wait_for(request, 15)
            assert stopped.json()['result']['task']['id'] == original['id']
            assert stopped.json()['result']['task']['status']['state'] == 'TASK_STATE_CANCELED', stopped.text
            assert [row['messageId'] for row in stopped.json()['result']['task']['history']] == [
                body['params']['message']['messageId'], task['status']['message']['messageId'], answer['params']['message']['messageId']]
            assert not (Path(get_settings().AGENT_DATA_DIR) / str(seed['agent']) / 'workspace/after-answer-stop.md').exists()
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                controls = list(await db.scalars(select(SessionControlInput).where(SessionControlInput.session_id == UUID(original['contextId']))))
                assert len(controls) == 1 and controls[0].expected_run_id == successor_id and controls[0].status == 'applied'
            assert len(model.requests) == 2
            return
        await _run_native_machine_task(seed, successor_id, app_user_sessionmaker)
        completed = await asyncio.wait_for(request, 15)
        assert completed.status_code == 200 and 'result' in completed.json(), completed.text
        result = completed.json()['result']['task']
        assert result['id'] == original['id'] and result['contextId'] == original['contextId']
        assert result['status']['state'] == 'TASK_STATE_COMPLETED' and result['artifacts'][0]['parts'] == [{'text': final}]
        history = result['history']
        assert len(history) == 4 and [row['role'] for row in history] == ['ROLE_USER', 'ROLE_AGENT', 'ROLE_USER', 'ROLE_AGENT']
        assert [row['messageId'] for row in history[:3]] == [body['params']['message']['messageId'],
            task['status']['message']['messageId'], answer['params']['message']['messageId']]
        assert history[-1]['parts'] == [{'text': final}]
        assert all(row['taskId'] == original['id'] and row['contextId'] == original['contextId'] for row in history)
        for count in (0, 1, 2, 10):
            read = await api.post(path, json={'jsonrpc': '2.0', 'id': 'history-' + str(count), 'method': 'GetTask',
                'params': {'id': original['id'], 'historyLength': count}}, headers=headers)
            assert read.status_code == 200 and 'result' in read.json(), read.text
            if count:
                assert read.json()['result']['history'] == history[-count:]
            else:
                assert 'history' not in read.json()['result']
        repeated = await api.post(path, json={**answer, 'id': 'repeated-answer'}, headers=headers)
        assert repeated.json()['result']['task']['id'] == original['id'] and len(model.requests) == 2
        later = _send('Start another task with this completed task ID.')
        later['params']['message']['taskId'] = original['id']
        assert (await api.post(path, json=later, headers=headers)).json()['error']['code'] == -32004
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            answered = list(await db.scalars(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.session_id == UUID(original['contextId']), ChatTranscriptEvent.item_kind == 'user_question',
                ChatTranscriptEvent.lifecycle == 'completed')))
            assert len(answered) == 1 and answered[0].metadata_json['actor'] == {
                'type': 'external_principal', 'id': identity['id']}
        wire = json.dumps(model.requests[-1], default=str)
        assert 'Use English for the external partner.' in wire and 'Prepare the partner launch report' in wire
        (tmp_path / 'machine-question-requests.json').write_text(json.dumps(model.requests, default=str))
    finally:
        release.set()
        work_items = (request, waiting, worker) if worker is not None else (request, waiting)
        for work in work_items:
            if not work.done():
                work.cancel()
        await asyncio.gather(*work_items, return_exceptions=True)


@pytest.mark.parametrize('cancel_queued', [False, True])
async def test_machine_answer_survives_transport_receipt_loss_and_terminal_boundary_delay(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, drain_terminal_boundary_for_task, cancel_queued,
):
    import app.runtime.invoker as invoker
    import app.services.a2a_machine_tasks as transport
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.services.runtime_task_worker import drain_web_terminal_boundary_outbox_once
    from app.services.session_input_dispatch import recover_admitted_session_inputs_once
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, api = machine_api
    await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed),
        'scopes': ['agent.execute', 'task.read', 'task.cancel']})
    identity = issued.json()
    headers = {'Authorization': 'Bearer ' + identity['token'], 'A2A-Version': '1.0'}
    path = f"/agents/{seed['agent']}/a2a/rpc"
    started = await api.post(path, json=_send('Prepare the launch after confirming the audience.'), headers=headers)
    original = started.json()['result']['task']
    root_id = UUID(original['id'])
    model = ScriptedLLMClient([{'tool_calls': [{'id': 'recovered-question', 'type': 'function', 'function': {
        'name': 'ask_user_question', 'arguments': json.dumps({'questions': [{'question': 'Which audience?',
            'options': [{'label': 'External partners', 'description': 'For partner distribution.'},
                {'label': 'Internal team', 'description': 'For internal review.'}]}]})}}], 'finish_reason': 'tool_calls'},
        {'content': 'The external partner launch is ready.', 'finish_reason': 'stop'}])
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: model)
    await _run_native_machine_task(seed, root_id, app_user_sessionmaker)
    paused = await api.post(path, json={'jsonrpc': '2.0', 'id': 'before-recovery', 'method': 'GetTask',
        'params': {'id': str(root_id)}}, headers=headers)
    assert paused.json()['result']['status']['state'] == 'TASK_STATE_INPUT_REQUIRED', paused.text
    answer = _send('External partners.')
    answer['params']['message']['taskId'] = str(root_id)
    answer['params']['metadata'] = {'credential': identity['token'], 'provider_key': 'synthetic-machine-only'}
    actual_insert = transport.insert
    fail_once = True
    def fail_transport_receipt(model_type):
        nonlocal fail_once
        if fail_once and model_type is ChannelIngressEvent:
            fail_once = False
            raise RuntimeError('synthetic_transport_receipt_crash')
        return actual_insert(model_type)
    monkeypatch.setattr(transport, 'insert', fail_transport_receipt)
    with pytest.raises(RuntimeError, match='synthetic_transport_receipt_crash'):
        unexpected = await api.post(path, json=answer, headers=headers)
        pytest.fail('Transport receipt fault did not execute: ' + unexpected.text)
    input_id = uuid5(UUID(identity['id']), 'a2a-message:' + answer['params']['message']['messageId'])
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        row = await db.get(SessionTurnInput, input_id)
        assert row is not None and row.intent == 'answer_request' and row.status == 'rolled_over'
        assert await db.get(ChannelIngressEvent, uuid5(uuid5(input_id, 'session-v2-runtime-run'), 'ingress')) is None
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.services.secrets_provider import get_secrets_provider

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed['agent'])
        configured = await db.get(LLMModel, agent.primary_model_id)
        configured.api_key_encrypted = get_secrets_provider().encrypt('synthetic-recovered-machine-only')
        await db.commit()
    before_replay = await api.post(path, json={'jsonrpc': '2.0', 'id': 'read-without-receipt', 'method': 'GetTask',
        'params': {'id': str(root_id)}}, headers=headers)
    history_before_replay = before_replay.json()['result']['history']
    assert [row['messageId'] for row in history_before_replay] == [started.json()['result']['task']['history'][0]['messageId'],
        paused.json()['result']['status']['message']['messageId'], answer['params']['message']['messageId']]
    replayed = await api.post(path, json=answer, headers=headers)
    assert replayed.status_code == 200 and replayed.json()['result']['task']['id'] == original['id'], replayed.text
    assert replayed.json()['result']['task']['status']['state'] == 'TASK_STATE_SUBMITTED', replayed.text
    assert replayed.json()['result']['task']['history'] == history_before_replay
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        ingress = await db.get(ChannelIngressEvent, uuid5(uuid5(input_id, 'session-v2-runtime-run'), 'ingress'))
        persisted = json.dumps(ingress.payload_json)
        assert identity['token'] not in persisted and 'synthetic-machine-only' not in persisted
    if cancel_queued:
        import asyncio
        from app.services.session_event_outbox import SessionEventOutboxPublisher
        from app.services.web_chat_runtime import web_chat_broker
        from app.services.web_chat_stream_bus import _user_live_projection, canonical_payload_hash

        delivered = []
        async def local_transport(delivery):
            envelope = delivery['envelope']
            assert canonical_payload_hash(envelope) == delivery['envelope_sha256']
            delivered.append(envelope.get('kind'))
            await web_chat_broker.send_session_message(delivery['agent_id'], delivery['session_id'],
                _user_live_projection(envelope))
        publisher = SessionEventOutboxPublisher(session_factory=app_user_sessionmaker)
        flushed = await publisher.drain_once(worker_id='machine-cancel-before-wait', tenant_id=seed['tenant'],
            publish_callback=local_transport)
        assert flushed['published'] > 0 and flushed['retried'] == 0 and flushed['dead_lettered'] == 0
        delivered.clear()
        connected = asyncio.Event()
        actual_connect = web_chat_broker.connect
        async def observe_connection(*args, **kwargs):
            await actual_connect(*args, **kwargs)
            connected.set()
        monkeypatch.setattr(web_chat_broker, 'connect', observe_connection)
        blocking = asyncio.create_task(api.post(path, json={**answer, 'params': {**answer['params'],
            'configuration': {'returnImmediately': False}}}, headers=headers))
        try:
            await asyncio.wait_for(connected.wait(), 15)
            cancel = {'jsonrpc': '2.0', 'id': 'cancel-queued', 'method': 'CancelTask', 'params': {'id': str(root_id)}}
            cancelled = await api.post(path, json=cancel, headers=headers)
            assert cancelled.json().get('result', {}).get('status', {}).get('state') == 'TASK_STATE_CANCELED', cancelled.text
            assert (await api.post(path, json=cancel, headers=headers)).json()['result']['status']['state'] == 'TASK_STATE_CANCELED'
            fresh = _send('Try to resume a cancelled task with a new message.')
            fresh['params']['message']['taskId'] = str(root_id)
            rejected = await api.post(path, json=fresh, headers=headers)
            assert rejected.json().get('error', {}).get('code') == -32004, rejected.text
            published = await publisher.drain_once(worker_id='machine-cancel-after-wait', tenant_id=seed['tenant'],
                publish_callback=local_transport)
            assert published['published'] > 0 and published['retried'] == 0 and published['dead_lettered'] == 0
            assert 'human_input.cancelled' in delivered
            returned = await asyncio.wait_for(blocking, 15)
            assert returned.json()['result']['task']['status']['state'] == 'TASK_STATE_CANCELED', returned.text
        finally:
            if not blocking.done():
                blocking.cancel()
            await asyncio.gather(blocking, return_exceptions=True)
    try:
        await drain_terminal_boundary_for_task(drain_web_terminal_boundary_outbox_once, task_id=root_id,
            worker_id='machine-delayed-question-boundary')
    except BaseException:
        from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox

        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            rows = list(await db.scalars(select(RuntimeTerminalBoundaryOutbox).where(
                RuntimeTerminalBoundaryOutbox.runtime_task_id == root_id)))
            (tmp_path / 'machine-delayed-boundary.json').write_text(json.dumps([
                {'status': row.status, 'last_error': row.last_error, 'binding': row.binding_json}
                for row in rows], default=str))
        raise
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == input_id))
        await recover_admitted_session_inputs_once(db, worker_id='recover-machine-answer', tenant_id=seed['tenant'],
            admission_ids=(admission.id,))
        successors = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.root_runtime_task_id == root_id, RuntimeTask.id != root_id)))
        if cancel_queued:
            assert not successors
            row = await db.get(SessionTurnInput, input_id, populate_existing=True)
            assert row.status == 'cancelled' and row.bound_round_id is None
            assert await transport._machine_question(db, await db.get(RuntimeTask, root_id)) is None
        else:
            assert len(successors) == 1
            successor_id = successors[0].id
    if cancel_queued:
        repeated = await api.post(path, json=answer, headers=headers)
        assert repeated.json()['result']['task']['status']['state'] == 'TASK_STATE_CANCELED', repeated.text
        assert len(model.requests) == 1
        return
    await _run_native_machine_task(seed, successor_id, app_user_sessionmaker)
    repeated = await api.post(path, json=answer, headers=headers)
    assert repeated.json()['result']['task']['status']['state'] == 'TASK_STATE_COMPLETED', repeated.text
    assert len(model.requests) == 2


@pytest.mark.parametrize('revoke_at', [None, 'before_effect', 'after_effect'])
async def test_machine_company_approval_executes_once_and_returns_to_original_task(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, drain_terminal_boundary_for_task, revoke_at,
):
    import app.runtime.invoker as invoker
    from app.api.chat_sessions import router
    from app.models.audit import ApprovalRequest
    from app.models.guard_policy import GuardPolicy
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, api = machine_api
    api._transport.app.include_router(router)
    root = await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    async with owner_sessionmaker() as db:
        db.add(GuardPolicy(tenant_id=seed['tenant'], zone_guard={'tool_rules': [
            {'tools': ['write_file'], 'decision': 'require_approval'}]}))
        await db.commit()
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed),
        'scopes': ['agent.execute', 'task.read']})
    assert issued.status_code == 201, issued.text
    identity = issued.json()
    headers = {'Authorization': 'Bearer ' + identity['token'], 'A2A-Version': '1.0'}
    path = f"/agents/{seed['agent']}/a2a/rpc"
    response = await api.post(path, json=_send('Write workspace/approved.md and return the report.'), headers=headers)
    original = response.json()['result']['task']
    task_id = UUID(original['id'])
    model = ScriptedLLMClient([{'tool_calls': [{'id': 'machine-approved-write', 'type': 'function', 'function': {
        'name': 'write_file', 'arguments': json.dumps({'path': 'workspace/approved.md', 'content': 'APPROVED_MACHINE_REPORT'})}}],
        'finish_reason': 'tool_calls'}, {'tool_calls': [{'id': 'read-approved-machine-report', 'type': 'function',
            'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'workspace/approved.md'})}}],
            'finish_reason': 'tool_calls'}, {'content': 'The approved machine report is ready.', 'finish_reason': 'stop'}])
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: model)
    await _run_native_machine_task(seed, task_id, app_user_sessionmaker)
    paused = await api.post(path, json={'jsonrpc': '2.0', 'id': 'paused', 'method': 'GetTask',
        'params': {'id': str(task_id)}}, headers=headers)
    assert paused.json()['result']['status']['state'] == 'TASK_STATE_AUTH_REQUIRED', paused.text
    assert not (root / 'workspace/approved.md').exists() and len(model.requests) == 1
    from app.services.runtime_task_worker import drain_web_terminal_boundary_outbox_once

    await drain_terminal_boundary_for_task(drain_web_terminal_boundary_outbox_once, task_id=task_id,
        worker_id='machine-approval-origin-boundary')
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        approval = await db.scalar(select(ApprovalRequest).where(ApprovalRequest.tenant_id == seed['tenant']))
        assert approval.requested_by is None and approval.requested_by_external_principal_id == UUID(identity['id'])
        approval_id = approval.id
        from app.models.session_v2 import SessionToolInvocation
        from app.models.chat_transcript_event import ChatTranscriptEvent

        invocation = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == task_id))
        event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        (tmp_path / 'machine-approval-origin.json').write_text(json.dumps({
            'envelope': approval.execution_envelope, 'input_hash': approval.input_hash, 'decision_id': approval.decision_id,
            'effective_arguments': invocation.effective_arguments_json, 'effective_args_hash': invocation.effective_args_hash,
            'event': event.metadata_json}, default=str, indent=2))
    decision_path = f"/agents/{seed['agent']}/sessions/{original['contextId']}/approvals/{approval_id}/resolve"
    decision = await api.post(decision_path, json={'action': 'approve'})
    assert decision.status_code == 200, decision.text
    assert (await api.post(decision_path, json={'action': 'approve'})).status_code == 200
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        claims = await RuntimeTaskClaimService(db=db, worker_id='machine-approved-effect',
            task_types=('approval_execution',)).claim_available()
        assert len(claims) == 1
        job_id, claim_version = claims[0].id, claims[0].claim_version
        job = await db.get(RuntimeTask, job_id)
        assert job.root_user_id is None and job.root_external_principal_id == UUID(identity['id'])
        assert job.machine_credential_id == UUID(identity['credential_id'])
    async def revoke():
        revoked = await api.post(f"/agents/{seed['agent']}/machine-identities/{identity['id']}/revoke", json={
            'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant'])})
        assert revoked.status_code == 200, revoked.text
    if revoke_at == 'before_effect':
        await revoke()
    elif revoke_at == 'after_effect':
        from app.services.agent_tools import _get_tool_runtime_service

        service = _get_tool_runtime_service()
        complete = service.approval_ticket_completer
        async def complete_then_revoke(**kwargs):
            await complete(**kwargs)
            await revoke()
        monkeypatch.setattr(service, 'approval_ticket_completer', complete_then_revoke)
    executed = await run_claimed_runtime_task(execute_claimed_approval_execution(job_id), task_id=job_id,
        claim_version=claim_version, worker_id='machine-approved-effect', lease_seconds=180)
    assert executed == ('needs_reapproval' if revoke_at == 'before_effect' else 'succeeded')
    if revoke_at is not None:
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            ticket = await db.get(ApprovalRequest, approval_id)
            if revoke_at == 'before_effect':
                assert ticket.consumed_at is None and ticket.execution_status == 'needs_reapproval'
                assert not (root / 'workspace/approved.md').exists()
            else:
                assert ticket.consumed_at is not None and ticket.execution_status == 'succeeded'
                assert ticket.execution_receipt['continuation_status'] == 'queued'
                assert (root / 'workspace/approved.md').read_text() == 'APPROVED_MACHINE_REPORT'
        if revoke_at == 'after_effect':
            service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
            claimed = await service.claim_batch(worker_id='revoked-machine-return', limit=100)
            pages = await service.prepare_integration_pages(worker_id='revoked-machine-return', claimed=claimed)
            assert len(pages) == 1
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as denied:
                await service._deliver_page(pages[0])
            assert denied.value.status_code in {401, 403}
        assert len(model.requests) == 1
        assert (await api.post(path, json={'jsonrpc': '2.0', 'id': 'revoked-read', 'method': 'GetTask',
            'params': {'id': str(task_id)}}, headers=headers)).status_code == 401
        return
    assert await execute_claimed_approval_execution(job_id) == 'succeeded'
    assert (root / 'workspace/approved.md').read_text() == 'APPROVED_MACHINE_REPORT'
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        from app.services.session_model_inputs import _origin_dependencies
        from app.models.session_v2 import SessionModelResult

        dependencies, complete = await _origin_dependencies(db, tenant_id=seed['tenant'], agent_id=seed['agent'],
            ref=f'session-tool-result:{invocation.id}')
        native = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == task_id))
        (tmp_path / 'machine-approval-sources.json').write_text(json.dumps({'complete': complete,
            'dependencies': dependencies, 'model_snapshot': native.model_request_snapshot_json}, default=str, indent=2))
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    claimed = await service.claim_batch(worker_id='machine-approval-return', limit=100)
    pages = await service.prepare_integration_pages(worker_id='machine-approval-return', claimed=claimed)
    assert len(pages) == 1 and pages[0].parent_user_id is None
    receipt = await service._deliver_page(pages[0])
    assert receipt.get('ok') is True, receipt
    assert await service._mark_page_delivered(page=pages[0], worker_id='machine-approval-return', receipt=receipt) == 1
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        successors = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.parent_session_id == original['contextId'],
            RuntimeTask.task_type != 'approval_execution', RuntimeTask.id != task_id)))
        assert len(successors) == 1
        successor = successors[0]
        assert successor.root_user_id is None and successor.root_external_principal_id == UUID(identity['id'])
        assert successor.root_runtime_task_id == task_id
        original_run = await db.get(RuntimeTask, task_id)
        assert successor.budget_run_id is not None and successor.budget_run_id == original_run.budget_run_id
    await _run_native_machine_task(seed, successor.id, app_user_sessionmaker)
    result = await api.post(path, json={'jsonrpc': '2.0', 'id': 'returned', 'method': 'GetTask',
        'params': {'id': str(task_id)}}, headers=headers)
    returned = result.json()['result']
    assert returned['id'] == original['id'] and returned['contextId'] == original['contextId']
    assert returned['status']['state'] == 'TASK_STATE_COMPLETED', result.text
    assert returned['artifacts'][0]['parts'] == [{'text': 'The approved machine report is ready.'}]
    assert len(model.requests) == 3 and 'APPROVED_MACHINE_REPORT' in json.dumps(model.requests[-1], default=str)
    (tmp_path / 'machine-approval-requests.json').write_text(json.dumps(model.requests, default=str))
    from app.models.token_usage_event import TokenUsageEvent

    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        usage = list(await db.scalars(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed['agent'],
            TokenUsageEvent.source == 'kernel')))
        # Existing billing charges the employee owner; it is not requester authority.
        assert usage and all(row.user_id == seed['owner'] for row in usage)
        current = await db.get(RuntimeTask, successor.id)
        assert current.root_user_id is None and current.root_external_principal_id == UUID(identity['id'])


@pytest.mark.parametrize('revoke_before_effect', [False, True])
async def test_machine_task_runs_native_kernel_tools_and_returns_full_original_result(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, revoke_before_effect,
):
    from datetime import datetime, timedelta, timezone
    import hashlib
    import app.runtime.invoker as invoker
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run
    from app.services.workspace_resource_authority import register_workspace_path
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, api = machine_api
    root = await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    canary = 'OWNER_PRIVATE_MACHINE_MUST_NOT_RECEIVE'
    private = root / 'workspace/owner-private.md'
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text(canary)
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        await register_workspace_path(db, tenant_id=seed['tenant'], agent_id=seed['agent'], path='workspace/owner-private.md',
            owner_user_id=seed['owner'], root_session_id=None, source='native_fixture',
            content_hash=hashlib.sha256(private.read_bytes()).hexdigest())
        await db.commit()
    grant = {**_issue(seed), 'scopes': ['agent.execute', 'task.read', 'task.cancel']}
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json=grant)
    assert issued.status_code == 201, issued.text
    headers = {'Authorization': 'Bearer ' + issued.json()['token'], 'A2A-Version': '1.0'}
    path = f"/agents/{seed['agent']}/a2a/rpc"
    submitted = await api.post(path, json=_send('Write workspace/machine-report.md, read it and return the complete report.'), headers=headers)
    assert 'result' in submitted.json(), submitted.text
    task_id = UUID(submitted.json()['result']['task']['id'])
    await _dispatch_machine_input(seed, task_id, app_user_sessionmaker)
    final = 'FULL_MACHINE_RESULT\n' + ('recoverable-original-result\n' * 1000)
    calls = [('read_file', {'path': 'workspace/owner-private.md'}),
        ('write_file', {'path': 'workspace/machine-report.md', 'content': 'MACHINE_REPORT'}),
        ('read_file', {'path': 'workspace/machine-report.md'})]
    script = [{'tool_calls': [{'id': f'machine-tool-{i}', 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments)}}], 'finish_reason': 'tool_calls'}
        for i, (name, arguments) in enumerate(calls)] + [{'content': final, 'finish_reason': 'stop'}]
    client = ScriptedLLMClient(script)
    if revoke_before_effect:
        stream = client.stream
        async def revoke_during_provider_delivery(**kwargs):
            result = await stream(**kwargs)
            if len(client.requests) == 2:
                revoked = await api.post(f"/agents/{seed['agent']}/machine-identities/{issued.json()['id']}/revoke", json={
                    'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant'])})
                assert revoked.status_code == 200, revoked.text
            return result
        client.stream = revoke_during_provider_delivery
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: client)
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id)
        task.status, task.claimed_by, task.claim_version, task.attempt_count = 'running', 'machine-native-worker', 1, 1
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.commit()
    fence = set_runtime_task_fence(task_id=task_id, worker_id='machine-native-worker', claim_version=1)
    try:
        await execute_web_chat_run(task_id)
    finally:
        reset_runtime_task_fence(fence)
    read = await api.post(path, json={'jsonrpc': '2.0', 'id': 2, 'method': 'GetTask', 'params': {'id': str(task_id)}}, headers=headers)
    if revoke_before_effect:
        from app.models.session_v2 import SessionToolInvocation
        assert read.status_code == 401
        assert not (root / 'workspace/machine-report.md').exists() and len(client.requests) == 2
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, task_id)
            assert task.status != 'completed' and task.root_user_id is None
            invocation = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == task_id,
                SessionToolInvocation.tool_name == 'write_file'))
            assert invocation is not None and invocation.effect_state == 'prepared_not_started'
        return
    result = read.json()['result']
    (tmp_path / 'machine-result.json').write_text(read.text)
    (tmp_path / 'machine-model-requests.json').write_text(json.dumps(client.requests, default=str))
    assert result['status']['state'] == 'TASK_STATE_COMPLETED', result
    assert any(part.get('text') == final for artifact in result['artifacts'] for part in artifact['parts'])
    assert (root / 'workspace/machine-report.md').read_text() == 'MACHINE_REPORT'
    assert len(client.requests) == 4 and client.requests[0]['tools']
    assert any(message.get('role') == 'user' and 'Write workspace/machine-report.md' in str(message.get('content'))
        for message in client.requests[0]['messages'])
    assert canary not in json.dumps(client.requests, default=str) and private.read_text() == canary


@pytest.mark.parametrize('cancel_separate', [False, True])
async def test_machine_cancel_returns_actual_pending_then_acknowledged_state(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, drain_terminal_boundary_for_task, cancel_separate,
):
    import asyncio
    from datetime import datetime, timedelta, timezone
    import app.kernel.engine as engine
    import app.runtime.invoker as invoker
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCommand, SessionControlInput, SessionTurnInput
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_task_worker import drain_web_terminal_boundary_outbox_once
    from app.services.web_chat_runtime import execute_web_chat_run
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, api = machine_api
    root = await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed),
        'scopes': ['agent.execute', 'task.read', 'task.cancel']})
    assert issued.status_code == 201, issued.text
    identity = issued.json()
    headers = {'Authorization': 'Bearer ' + identity['token'], 'A2A-Version': '1.0'}
    path = f"/agents/{seed['agent']}/a2a/rpc"
    submitted = await api.post(path, json=_send('Write workspace/after-stop.md and return it.'), headers=headers)
    task_id = UUID(submitted.json()['result']['task']['id'])
    entered, release = asyncio.Event(), asyncio.Event()
    actual_tool = engine._execute_tool_call_with_cancel
    async def held_tool(*args, **kwargs):
        entered.set()
        await release.wait()
        return await actual_tool(*args, **kwargs)
    monkeypatch.setattr(engine, '_execute_tool_call_with_cancel', held_tool)
    separate_final = 'The separate work item completed under its own task identity.'
    model = ScriptedLLMClient([{'tool_calls': [{'id': 'cancelled-write', 'type': 'function', 'function': {
        'name': 'write_file', 'arguments': json.dumps({'path': 'workspace/after-stop.md', 'content': 'MUST_NOT_EXECUTE'})}}],
        'finish_reason': 'tool_calls'}, {'content': separate_final, 'finish_reason': 'stop'}])
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: model)
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id)
        task.status, task.claimed_by, task.claim_version, task.attempt_count = 'running', 'machine-cancel-worker', 1, 1
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.commit()
    async def run():
        fence = set_runtime_task_fence(task_id=task_id, worker_id='machine-cancel-worker', claim_version=1)
        try:
            await execute_web_chat_run(task_id)
        finally:
            reset_runtime_task_fence(fence)
    worker = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 15)
        separate = _send('Prepare a separate work item while this task is running.')
        separate['params']['message']['contextId'] = submitted.json()['result']['task']['contextId']
        new_work = await api.post(path, json=separate, headers=headers)
        assert 'result' in new_work.json(), new_work.text
        separate_task = new_work.json()['result']['task']
        assert separate_task['id'] != str(task_id)
        assert separate_task['contextId'] == separate['params']['message']['contextId']
        assert separate_task['status']['state'] == 'TASK_STATE_SUBMITTED', new_work.text
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            new_input = await db.get(SessionTurnInput, uuid5(UUID(identity['id']),
                'a2a-message:' + separate['params']['message']['messageId']))
            assert new_input.intent == 'queue_next_turn' and new_input.target_run_id is None
            assert new_input.request_item_id is None
        if cancel_separate:
            stopped_separate = await api.post(path, json={'jsonrpc': '2.0', 'id': 'cancel-separate', 'method': 'CancelTask',
                'params': {'id': separate_task['id']}}, headers=headers)
            assert stopped_separate.json()['result']['status']['state'] == 'TASK_STATE_CANCELED', stopped_separate.text
        else:
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                receipt = await db.get(ChannelIngressEvent, uuid5(UUID(separate_task['id']), 'ingress'))
                await db.delete(receipt)  # Admission committed before its transport receipt survived.
                await db.commit()
        replay_separate = await api.post(path, json=separate, headers=headers)
        assert replay_separate.json()['result']['task']['id'] == separate_task['id'], replay_separate.text
        assert replay_separate.json()['result']['task']['status']['state'] == (
            'TASK_STATE_CANCELED' if cancel_separate else 'TASK_STATE_SUBMITTED')
        assert replay_separate.json()['result']['task']['history'] == [
            {**separate['params']['message'], 'taskId': separate_task['id']}]
        original = await api.post(path, json={'jsonrpc': '2.0', 'id': 'read-original', 'method': 'GetTask',
            'params': {'id': str(task_id)}}, headers=headers)
        assert original.json()['result']['status']['state'] == 'TASK_STATE_WORKING', original.text
        assert separate['params']['message']['messageId'] not in [row['messageId']
            for row in original.json()['result']['history']]
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            assert await db.scalar(select(func.count(SessionControlInput.id)).where(
                SessionControlInput.expected_run_id == task_id)) == 0
        body = {'jsonrpc': '2.0', 'id': 'stop-1', 'method': 'CancelTask', 'params': {'id': str(task_id)}}
        cancelled = await api.post(path, json=body, headers=headers)
        assert cancelled.status_code == 200 and 'result' in cancelled.json(), cancelled.text
        assert cancelled.json()['result']['status']['state'] == 'TASK_STATE_WORKING'
        assert cancelled.json()['result']['metadata']['hive']['cancellationStatus'] == 'applying'
        repeated = await api.post(path, json={**body, 'id': 'stop-2'}, headers=headers)
        assert repeated.status_code == 200 and repeated.json()['result']['status']['state'] == 'TASK_STATE_WORKING'
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            controls = list(await db.scalars(select(SessionControlInput).where(SessionControlInput.expected_run_id == task_id)))
            assert len(controls) == 1 and controls[0].status == 'applying'
            command = await db.get(SessionCommand, controls[0].command_id)
            assert command.principal_type == 'external_principal' and command.principal_id == UUID(identity['id'])
            accepted = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.command_id == command.id,
                ChatTranscriptEvent.item_kind == 'control_input', ChatTranscriptEvent.lifecycle == 'accepted'))
            assert accepted.metadata_json['actor'] == {'type': 'external_principal', 'id': identity['id']}
        release.set()
        await asyncio.wait_for(worker, 15)
        result = await api.post(path, json={'jsonrpc': '2.0', 'id': 'read-stop', 'method': 'GetTask',
            'params': {'id': str(task_id)}}, headers=headers)
        assert result.json()['result']['status']['state'] == 'TASK_STATE_CANCELED', result.text
        assert not (root / 'workspace/after-stop.md').exists() and len(model.requests) == 1
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            control = await db.scalar(select(SessionControlInput).where(SessionControlInput.expected_run_id == task_id))
            assert control.status == 'applied'
        if not cancel_separate:
            await drain_terminal_boundary_for_task(drain_web_terminal_boundary_outbox_once, task_id=task_id,
                worker_id='machine-queued-work-boundary')
            separate_id = UUID(separate_task['id'])
            await _dispatch_machine_input(seed, separate_id, app_user_sessionmaker)
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                separate_run, original_run = await db.get(RuntimeTask, separate_id), await db.get(RuntimeTask, task_id)
                assert separate_run.root_runtime_task_id == separate_id and separate_run.status == 'pending'
                assert separate_run.budget_run_id != original_run.budget_run_id
                assert separate_run.root_external_principal_id == UUID(identity['id']) and separate_run.root_user_id is None
            await _run_native_machine_task(seed, separate_id, app_user_sessionmaker)
            delivered = await api.post(path, json={'jsonrpc': '2.0', 'id': 'read-separate', 'method': 'GetTask',
                'params': {'id': separate_task['id']}}, headers=headers)
            completed = delivered.json()['result']
            assert completed['status']['state'] == 'TASK_STATE_COMPLETED', delivered.text
            assert completed['artifacts'][0]['parts'] == [{'text': separate_final}]
            assert len(completed['history']) == 2 and completed['history'][0] == {
                **separate['params']['message'], 'taskId': separate_task['id']}
            assert completed['history'][1]['parts'] == [{'text': separate_final}]
            assert all(row['taskId'] == separate_task['id'] for row in completed['history'])
            assert len(model.requests) == 2 and not (root / 'workspace/after-stop.md').exists()
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_blocking_machine_send_waits_for_committed_result_and_replays_once(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import asyncio
    from datetime import datetime, timedelta, timezone
    import app.runtime.invoker as invoker
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, api = machine_api
    await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed),
        'scopes': ['agent.execute', 'task.read']})
    assert issued.status_code == 201, issued.text
    headers = {'Authorization': 'Bearer ' + issued.json()['token'], 'A2A-Version': '1.0'}
    path = f"/agents/{seed['agent']}/a2a/rpc"
    body = _send('Return the complete partner note.')
    body['params'].pop('configuration')  # The protocol default is blocking.
    final = 'A complete, durable machine response.'
    model = ScriptedLLMClient([{'content': final, 'finish_reason': 'stop'}])
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: model)
    notified, notification = asyncio.Event(), {}
    async def wake(**kwargs):
        notification.update(kwargs)
        notified.set()
    monkeypatch.setattr('app.services.runtime_task_worker.notify_runtime_task_worker', wake)
    request = asyncio.create_task(api.post(path, json=body, headers=headers))
    waiting = asyncio.create_task(notified.wait())
    try:
        await asyncio.wait((request, waiting), timeout=15, return_when=asyncio.FIRST_COMPLETED)
        assert not request.done(), (await request).text
        assert notified.is_set()
        task_id = UUID(str(notification['runtime_task_id']))
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, task_id)
            task.status, task.claimed_by, task.claim_version, task.attempt_count = 'running', 'machine-blocking-worker', 1, 1
            task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
        fence = set_runtime_task_fence(task_id=task_id, worker_id='machine-blocking-worker', claim_version=1)
        try:
            await execute_web_chat_run(task_id)
        finally:
            reset_runtime_task_fence(fence)
        response = await asyncio.wait_for(request, 15)
        assert response.status_code == 200 and 'result' in response.json(), response.text
        task = response.json()['result']['task']
        assert task['status']['state'] == 'TASK_STATE_COMPLETED' and task['id'] == str(task_id)
        assert task['artifacts'][0]['parts'] == [{'text': final}]
        repeated = await api.post(path, json={**body, 'id': 'retry-same-message'}, headers=headers)
        assert repeated.json()['result']['task']['id'] == str(task_id) and len(model.requests) == 1
    finally:
        for work in (request, waiting):
            if not work.done():
                work.cancel()
        await asyncio.gather(request, waiting, return_exceptions=True)
