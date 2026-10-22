"""Machine commands use their own live credential, including terminal delivery."""

import asyncio
import json
from uuid import UUID

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.execution_environment import EnvironmentLease, ExecutionEnvironment
from app.models.runtime_task import RuntimeTask
from tests.integration.test_m3i_machine_tasks_postgres import (
    _dispatch_machine_input,
    _issue,
    _prepare_native_machine,
    _run_native_machine_task,
    _send,
    machine_api as _machine_api,
)

machine_api = _machine_api
pytestmark = pytest.mark.usefixtures('migrated_pg_url')


async def _start(seed, api, app_user_sessionmaker):
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json={
        **_issue(seed), 'scopes': ['agent.execute', 'task.read']})
    assert issued.status_code == 201, issued.text
    identity = issued.json()
    headers = {'Authorization': 'Bearer ' + identity['token'], 'A2A-Version': '1.0'}
    rpc = f"/agents/{seed['agent']}/a2a/rpc"
    submitted = await api.post(rpc, json=_send('Run the authorized local command and return its result.'), headers=headers)
    assert 'result' in submitted.json(), submitted.text
    task_id = UUID(submitted.json()['result']['task']['id'])
    await _dispatch_machine_input(seed, task_id, app_user_sessionmaker)
    return identity, headers, rpc, task_id


def _sandbox(monkeypatch):
    for key, value in {'HIVE_CODE_EXEC_PROVIDER': 'local_os_sandbox', 'HIVE_CODE_SANDBOX_MODE': 'auto',
        'HIVE_ALLOW_UNSANDBOXED_CODE_EXEC': '0'}.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize('revoke_inflight', [False, True])
async def test_machine_kernel_command_uses_live_independent_identity(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, revoke_inflight,
):
    import app.runtime.invoker as invoker
    from contextlib import contextmanager
    from app.models.capability_policy import CapabilityPolicy
    from app.services.agent_tool_domains import code_exec
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, api = machine_api
    root = await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    _sandbox(monkeypatch)
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        db.add(CapabilityPolicy(tenant_id=seed['tenant'], capability='workspace.command.execute',
            allowed=True, requires_approval=False))
        await db.commit()
    identity, headers, rpc, task_id = await _start(seed, api, app_user_sessionmaker)
    target = root / 'workspace/machine-command.txt'
    command = ('for i in $(seq 1 200); do printf MACHINE_EFFECT >> machine-command.txt; sleep 0.05; done'
        if revoke_inflight else 'printf MACHINE_EFFECT > machine-command.txt; cat machine-command.txt')
    client = ScriptedLLMClient([{'tool_calls': [{'id': 'machine-shell', 'type': 'function', 'function': {
        'name': 'run_command', 'arguments': json.dumps({'command': command, 'timeout': 30})}}],
        'finish_reason': 'tool_calls'}, {'content': 'MACHINE_COMMAND_RESULT', 'finish_reason': 'stop'}])
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: client)
    execution_paths = []
    materialize = code_exec.authorized_execution_workspace

    @contextmanager
    def observe_workspace(*args, **kwargs):
        with materialize(*args, **kwargs) as workspace:
            execution_paths.append(workspace / 'workspace/machine-command.txt')
            yield workspace

    monkeypatch.setattr(code_exec, 'authorized_execution_workspace', observe_workspace)
    running = asyncio.create_task(_run_native_machine_task(seed, task_id, app_user_sessionmaker))
    try:
        if revoke_inflight:
            async with asyncio.timeout(25):
                while not any(p.exists() and p.stat().st_size for p in execution_paths) and not running.done():
                    await asyncio.sleep(0.02)
            assert any(p.exists() and p.stat().st_size for p in execution_paths), 'The real machine command never started.'
            revoked = await api.post(f"/agents/{seed['agent']}/machine-identities/{identity['id']}/revoke", json={
                'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant'])})
            assert revoked.status_code == 200, revoked.text
        await running
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    (tmp_path / 'machine-command-model-requests.json').write_text(json.dumps(client.requests, default=str))
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id)
        leases = list(await db.scalars(select(EnvironmentLease).where(EnvironmentLease.runtime_task_id == task_id)))
        assert task.root_user_id is None and task.root_external_principal_id == UUID(identity['id'])
        assert task.machine_credential_id == UUID(identity['credential_id'])
        assert len(leases) == 1 and leases[0].principal_id is None and leases[0].status == 'released'
        assert leases[0].outcome['machine_principal'] == {
            'credential_id': identity['credential_id'], 'external_principal_id': identity['id']}
        if revoke_inflight:
            assert task.status != 'completed' and leases[0].outcome['status'] == 'cancelled'
        else:
            assert task.status == 'completed' and leases[0].outcome['status'] == 'completed'
    assert target.exists()
    if revoke_inflight:
        length = target.stat().st_size
        await asyncio.sleep(0.2)
        assert target.stat().st_size == length and 0 < length < len('MACHINE_EFFECT') * 200
        read = await api.post(rpc, json={'jsonrpc': '2.0', 'id': 'read', 'method': 'GetTask',
            'params': {'id': str(task_id)}}, headers=headers)
        assert read.status_code == 401
    else:
        assert target.read_text() == 'MACHINE_EFFECT'
        assert 'MACHINE_EFFECT' in json.dumps(client.requests[1]['messages'], default=str)
        read = await api.post(rpc, json={'jsonrpc': '2.0', 'id': 'read', 'method': 'GetTask',
            'params': {'id': str(task_id)}}, headers=headers)
        assert read.json()['result']['status']['state'] == 'TASK_STATE_COMPLETED'


async def test_machine_installed_setup_command_uses_invocation_identity(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import app.runtime.invoker as invoker
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec
    from app.runtime.hooks import HookEvent, hook_registry
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, api = machine_api
    await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    _sandbox(monkeypatch)
    identity, _headers, _rpc, task_id = await _start(seed, api, app_user_sessionmaker)
    client = ScriptedLLMClient([{'content': 'MACHINE_SETUP_RESULT', 'finish_reason': 'stop'}])
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: client)
    home = tmp_path / 'setup-home'
    home.mkdir()
    runner = GovernedHookRunner(policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_env_vars={'HOME', 'PATH'}))
    handler = runner.build_handler(HookSpec(key='machine-setup', event=HookEvent.SETUP, type='command',
        command='printf MACHINE_SETUP_EFFECT > machine-setup.txt', env={'HOME': str(home), 'PATH': '/usr/bin:/bin'},
        failure_mode='advisory'))
    hook_registry.register(HookEvent.SETUP, handler, key='machine-setup', failure_mode='advisory')
    try:
        await _run_native_machine_task(seed, task_id, app_user_sessionmaker)
    finally:
        hook_registry.unregister(HookEvent.SETUP, handler)
    assert (tmp_path / 'machine-setup.txt').read_text() == 'MACHINE_SETUP_EFFECT'
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id)
        assert task.root_user_id is None and task.status == 'completed' and len(client.requests) == 1
        leases = list(await db.scalars(select(EnvironmentLease).where(EnvironmentLease.runtime_task_id == task_id)))
        assert len(leases) == 1 and leases[0].principal_id is None and leases[0].status == 'released'
        assert leases[0].outcome['machine_principal'] == {
            'credential_id': identity['credential_id'], 'external_principal_id': identity['id']}


@pytest.mark.parametrize('state', ['ready', 'stopped', 'revoked'])
async def test_machine_installed_command_uses_canonical_terminal_claim(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, state,
):
    import app.runtime.invoker as invoker
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec
    from app.runtime.hooks import HookEvent, hook_registry
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, api = machine_api
    await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    _sandbox(monkeypatch)
    identity, _headers, _rpc, task_id = await _start(seed, api, app_user_sessionmaker)
    client = ScriptedLLMClient([{'content': 'COMMITTED_MACHINE_RESULT', 'finish_reason': 'stop'}])
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: client)
    await _run_native_machine_task(seed, task_id, app_user_sessionmaker)
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        assert (await db.get(RuntimeTask, task_id)).status == 'completed'
        db.add(ExecutionEnvironment(tenant_id=seed['tenant'], agent_id=seed['agent'], provider_key='local_os_sandbox',
            desired_state='stopped' if state == 'stopped' else 'ready', observed_state='stopped' if state == 'stopped' else 'ready'))
        await db.commit()
    if state == 'revoked':
        revoked = await api.post(f"/agents/{seed['agent']}/machine-identities/{identity['id']}/revoke", json={
            'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant'])})
        assert revoked.status_code == 200, revoked.text
    home = tmp_path / 'hook-home'
    home.mkdir()
    events = []
    runner = GovernedHookRunner(policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_env_vars={'HOME', 'PATH'}),
        transcript_writer=events.append, span_recorder=events.append)
    handler = runner.build_handler(HookSpec(key='machine-terminal', event=HookEvent.RESPONSE_COMPLETE, type='command',
        command='printf MACHINE_TERMINAL_EFFECT > machine-terminal.txt', env={'HOME': str(home), 'PATH': '/usr/bin:/bin'},
        failure_mode='advisory'))
    processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=tmp_path / 'machine-agents')
    service = RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker)
    hook_registry.register(HookEvent.RESPONSE_COMPLETE, handler, key='machine-terminal', failure_mode='advisory')
    try:
        counts = await service.drain_once(tenant_id=seed['tenant'], worker_id='machine-terminal-consumer',
            canonical_validator=processor.validate, process_callback=processor)
    finally:
        hook_registry.unregister(HookEvent.RESPONSE_COMPLETE, handler)
    (tmp_path / 'machine-terminal-hook-evidence.json').write_text(json.dumps(events, default=str))
    assert counts == {'claimed': 1, 'delivered': 1, 'retried': 0, 'dead_lettered': 0}, counts
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        row = await db.scalar(select(RuntimeTerminalBoundaryOutbox).where(RuntimeTerminalBoundaryOutbox.runtime_task_id == task_id))
        assert row.status == 'delivered' and row.delivery_receipt_json['response_projection_sha256']
        task = await db.get(RuntimeTask, task_id)
        assert task.root_user_id is None and task.machine_credential_id == UUID(identity['credential_id'])
        leases = list(await db.scalars(select(EnvironmentLease).where(EnvironmentLease.runtime_task_id == task_id)))
        if state == 'ready':
            assert (tmp_path / 'machine-terminal.txt').read_text() == 'MACHINE_TERMINAL_EFFECT'
            assert len(leases) == 1 and leases[0].principal_id is None and leases[0].status == 'released'
            assert leases[0].outcome['source']['boundary_id'] == str(row.id)
        else:
            assert not (tmp_path / 'machine-terminal.txt').exists() and leases == []
