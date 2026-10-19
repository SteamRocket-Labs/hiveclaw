"""Standard A2A discovery consumes a machine grant instead of a personal login."""
from uuid import UUID, uuid4

import pytest

from tests.integration.test_m3h_machine_identity_postgres import machine_api as _machine_api, _issue
from tests.integration.test_m3i_machine_tasks_postgres import _prepare_native_machine, _run_native_machine_task, _send

machine_api = _machine_api
pytestmark = pytest.mark.usefixtures('migrated_pg_url')


async def test_standard_agent_card_uses_machine_discovery_grant_without_expanding_it(machine_api):
    from a2a.types.a2a_pb2 import AgentCard
    from google.protobuf.json_format import ParseDict

    seed, api = machine_api
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json=_issue(seed))
    assert issued.status_code == 201, issued.text
    identity = issued.json()
    headers = {'Authorization': 'Bearer ' + identity['token']}
    path = f"/agents/{seed['agent']}/a2a/.well-known/agent-card.json"
    response = await api.get(path, headers=headers)
    assert response.status_code == 200, response.text
    card = ParseDict(response.json(), AgentCard())  # Strict official schema, with no legacy-field adapter.
    assert card.name and card.description and card.version and len(card.skills) == 1
    assert card.skills[0].id == 'hive.digital_employee' and card.skills[0].tags
    interface, = card.supported_interfaces
    assert interface.url == f"http://local/agents/{seed['agent']}/a2a/rpc"
    assert interface.protocol_binding == 'JSONRPC' and interface.protocol_version == '1.0'
    assert interface.tenant == '' and 'tenant' not in response.json()['supportedInterfaces'][0]
    assert not card.capabilities.streaming and not card.capabilities.push_notifications
    assert not card.capabilities.extended_agent_card
    assert list(card.default_input_modes) == ['text/plain']
    assert 'text/plain' in card.default_output_modes and 'application/octet-stream' in card.default_output_modes
    assert card.security_schemes['machineBearer'].http_auth_security_scheme.scheme == 'Bearer'
    assert len(card.security_requirements) == 1
    assert list(card.security_requirements[0].schemes) == ['machineBearer']
    assert list(card.security_requirements[0].schemes['machineBearer'].list) == []
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['a2a-version'] == '1.0'
    assert all(value not in response.text for value in (identity['token'], str(seed['owner']), str(seed['tenant']), identity['id']))
    denied = await api.post(interface.url, json=_send(), headers={**headers, 'A2A-Version': '1.0'})
    assert denied.status_code == 403
    assert (await api.get(path)).status_code == 401  # The fixture's personal JWT is not a machine credential.
    assert (await api.get(path, headers={'Authorization': ''})).status_code == 401
    assert (await api.get(path.replace(str(seed['agent']), str(uuid4())), headers=headers)).status_code == 403
    execute_only = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed), 'scopes': ['agent.execute']})
    assert execute_only.status_code == 201
    assert (await api.get(path, headers={'Authorization': 'Bearer ' + execute_only.json()['token']})).status_code == 403
    description = await api.get(f"/agents/{seed['agent']}/machine-description", headers=headers)
    assert description.json()['interoperability']['a2a_tasks']['agent_card_url'] == str(response.url)
    revoked = await api.post(f"/agents/{seed['agent']}/machine-identities/{identity['id']}/revoke", json={
        'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant'])})
    assert revoked.status_code == 200
    assert (await api.get(path, headers=headers)).status_code == 401


async def test_declared_optional_capabilities_return_standard_unsupported_errors(machine_api):
    from a2a.client import ClientConfig, ClientFactory
    from a2a.types import a2a_pb2 as protocol
    from a2a.utils.errors import PushNotificationNotSupportedError

    seed, api = machine_api
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed),
        'scopes': ['agent.describe', 'agent.execute', 'task.read']})
    assert issued.status_code == 201, issued.text
    headers = {'Authorization': 'Bearer ' + issued.json()['token'], 'A2A-Version': '1.0'}
    path = f"/agents/{seed['agent']}/a2a/rpc"
    unsupported = {
        'SendStreamingMessage': -32004, 'SubscribeToTask': -32004, 'GetExtendedAgentCard': -32004,
        'CreateTaskPushNotificationConfig': -32003, 'GetTaskPushNotificationConfig': -32003,
        'ListTaskPushNotificationConfigs': -32003, 'DeleteTaskPushNotificationConfig': -32003,
    }
    for method, code in unsupported.items():
        request = {'jsonrpc': '2.0', 'id': method, 'method': method, 'params': {'id': str(uuid4())}}
        response = await api.post(path, json=request, headers=headers)
        assert response.status_code == 200 and response.json()['error']['code'] == code, response.text
        assert (await api.post(path, json=request, headers={'Authorization': ''})).status_code == 401
    api.headers.update(headers)
    client = await ClientFactory(ClientConfig(httpx_client=api)).create_from_url(f"http://local/agents/{seed['agent']}/a2a")
    with pytest.raises(PushNotificationNotSupportedError):
        await client.get_task_push_notification_config(protocol.GetTaskPushNotificationConfigRequest(task_id=str(uuid4()), id='test'))


@pytest.mark.parametrize('prefix', ['/api', '/api/v1'])
async def test_official_sdk_discovers_and_consumes_machine_work_over_real_http(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, prefix,
):
    import asyncio
    import json
    import socket
    import httpx
    import uvicorn
    from a2a.client import A2AClientError, ClientConfig, ClientFactory
    from a2a.types import a2a_pb2 as protocol
    import app.runtime.invoker as invoker
    from app.database import get_db, tenant_scoped_session
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    seed, api = machine_api
    await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    issued = await api.post(f"/agents/{seed['agent']}/machine-identities", json={**_issue(seed),
        'scopes': ['agent.describe', 'agent.execute', 'task.read', 'task.cancel']})
    assert issued.status_code == 201, issued.text
    identity = issued.json()
    content = 'SDK_REPORT\n' + ('Complete captured source and result.\n' * 900)
    final = 'DELIVERABLE: workspace/sdk-report.md\n[Report](workspace/sdk-report.md)'
    model = ScriptedLLMClient([
        {'tool_calls': [{'id': 'sdk-write', 'type': 'function', 'function': {'name': 'write_file',
            'arguments': json.dumps({'path': 'workspace/sdk-report.md', 'content': content})}}], 'finish_reason': 'tool_calls'},
        {'content': final, 'finish_reason': 'stop'},
    ])
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: model)
    from app.main import app as web
    async def database():
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            yield db
    monkeypatch.setitem(web.dependency_overrides, get_db, database)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(web, host='127.0.0.1', port=port,
        ws='none', lifespan='off', log_level='warning', access_log=False))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            if serving.done():
                await serving
            await asyncio.sleep(0.02)
        assert server.started
        origin = f'http://127.0.0.1:{port}'
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + identity['token']}, timeout=15,
            trust_env=False) as wire:
            factory = ClientFactory(ClientConfig(httpx_client=wire, streaming=False, polling=True))
            client = await factory.create_from_url(f"{origin}{prefix}/agents/{seed['agent']}/a2a")
            request = protocol.SendMessageRequest(message=protocol.Message(message_id=str(uuid4()), role=protocol.ROLE_USER,
                parts=[protocol.Part(text='Write workspace/sdk-report.md and deliver the complete captured report.')]))
            events = [event async for event in client.send_message(request)]
            assert len(events) == 1 and events[0].HasField('task')
            task = events[0].task
            assert task.status.state == protocol.TASK_STATE_SUBMITTED
            assert task.history[0].message_id == request.message.message_id
            await _run_native_machine_task(seed, UUID(task.id), app_user_sessionmaker)
            result = await client.get_task(protocol.GetTaskRequest(id=task.id))
            assert result.id == task.id and result.context_id == task.context_id
            assert result.status.state == protocol.TASK_STATE_COMPLETED
            assert result.history[0].message_id == request.message.message_id
            assert result.artifacts[0].parts[0].text == final
            file_part, = [part for artifact in result.artifacts for part in artifact.parts if part.url]
            assert file_part.url.startswith(f"{origin}{prefix}/agents/{seed['agent']}/a2a/tasks/{task.id}/artifacts/")
            downloaded = await wire.get(file_part.url)
            assert downloaded.status_code == 200 and downloaded.content == content.encode(), downloaded.text
            assert downloaded.headers['cache-control'] == 'private, no-store'
            queued = protocol.SendMessageRequest(message=protocol.Message(message_id=str(uuid4()), role=protocol.ROLE_USER,
                context_id=task.context_id, parts=[protocol.Part(text='Prepare a separate report after this work.')]))
            queued_events = [event async for event in client.send_message(queued)]
            pending = queued_events[0].task
            assert pending.id != task.id and pending.context_id == task.context_id
            stopped = await client.cancel_task(protocol.CancelTaskRequest(id=pending.id))
            assert stopped.id == pending.id and stopped.status.state == protocol.TASK_STATE_CANCELED
            assert (await client.get_task(protocol.GetTaskRequest(id=task.id))).status.state == protocol.TASK_STATE_COMPLETED
            assert len(model.requests) == 2
            revoked = await api.post(f"/agents/{seed['agent']}/machine-identities/{identity['id']}/revoke", json={
                'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant'])})
            assert revoked.status_code == 200
            with pytest.raises(A2AClientError) as denied:
                await client.get_task(protocol.GetTaskRequest(id=task.id))
            assert isinstance(denied.value.__cause__, httpx.HTTPStatusError)
            assert denied.value.__cause__.response.status_code == 401
            assert (await wire.get(file_part.url)).status_code == 401
            await client.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
