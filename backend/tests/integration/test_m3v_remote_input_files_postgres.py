"""Owner uploads become actual SDK bytes with recoverable historical versions."""

import base64
import hashlib
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.chat_artifact import ChatArtifact
from app.models.runtime_task import RuntimeTask
from tests.integration import test_m3t_remote_chat_postgres as original
from tests.integration.test_m3u_remote_sdk_context_postgres import sdk_receiver
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
pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('strict', [False, True])
async def test_actual_uploads_reach_sdk_and_keep_previous_versions_beside_new_input(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path, strict
):
    from app.api import chat_sessions, files, upload
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

    seed, api, session_id = remote_work
    for router in (chat_sessions.router, files.router, upload.router):
        api._transport.app.include_router(router)
    await original.prepare_employee(seed, app_user_sessionmaker, monkeypatch, tmp_path)
    monkeypatch.setattr(upload, 'WORKSPACE_ROOT', tmp_path)
    bodies = [b'BINARY_SOURCE\x00\xff\r\n' * 3000, ('完整文件\r\n' * 4000).encode()]
    names = ['data]report.bin', 'source.txt']
    uploaded = []
    for name, body in zip(names, bodies):
        result = await api.post('/chat/upload', files={'file': (name, body, 'application/octet-stream')},
            data={'agent_id': str(seed['agent']), 'skip_personal_kb': 'true'})
        assert result.status_code == 200, result.text
        uploaded.append(result.json()['workspace_path'])
    base = f"/agents/{seed['agent']}/sessions/{session_id}"
    expected = {}
    async with sdk_receiver(tmp_path / 'receiver', strict) as receiver:
        path = await original.original.configure(seed, api, receiver)
        assert (await api.post(path + '/runtime', json=selection(seed))).status_code == 200
        for turn in range(2):
            input_id = uuid4()
            indexes = [0, 1] if turn == 0 else [1]
            if turn:
                changed = b'CURRENT_HUMAN_VERSION\n'
                saved = await api.put(f"/agents/{seed['agent']}/files/content", params={'path': uploaded[1]},
                    json={'content': changed.decode(), 'expected_current_hash': hashlib.sha256(bodies[1]).hexdigest()})
                assert saved.status_code == 200, saved.text
                bodies[1] = changed
            for part_index, index in enumerate(indexes, 1):
                expected[(str(input_id), part_index)] = (names[index], bodies[index])
            sent = await api.post(base + '/runs', json={'content': 'Consume the complete files and compare their versions.',
                'input_id': str(input_id), 'idempotency_key': f'remote-sdk-files-{turn}',
                'attachments': [{'name': names[index], 'path': uploaded[index]} for index in indexes]})
            assert sent.status_code == 201, sent.text
            run_id = sent.json()['run_id']
            await original.execute_chat(seed, run_id, app_user_sessionmaker)
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                task = await db.get(RuntimeTask, UUID(run_id))
                assert task.status == 'completed', task.metadata_json
                params = task.metadata_json['remote_a2a']['operations'][-1]['params']
            consumed = json.loads((receiver.directory / f'consumed-{turn}.json').read_text())
            observed = {(item['input_id'], item['part_index']): item for item in consumed['files']}
            assert observed.keys() == expected.keys()
            context = json.loads(params['message']['parts'][1]['text'])
            assert len(context['attachments']) == len(expected)
            for key, (name, body) in expected.items():
                assert observed[key]['filename'] == name
                assert base64.b64decode(observed[key]['body'], validate=True) == body
                assert observed[key]['sha256'] == hashlib.sha256(body).hexdigest()
            assert len(params['message']['parts']) == 2 + len(expected)
            returned = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='sdk-files')
            assert returned['delivered'] >= 1, returned
            report = (receiver.directory / f'report-{turn}.txt').read_text()
            assert json.loads(report)['files'] == consumed['files']
            assert any(item.get('content') == report for item in (await api.get(base + '/transcript')).json())
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == UUID(run_id),
                    ChatArtifact.session_id == session_id))
                assert artifact is not None
                artifact_id = artifact.id
            returned_file = await api.get(f"/agents/{seed['agent']}/files/artifacts/{artifact_id}/content")
            assert returned_file.status_code == 200 and returned_file.json()['content'] == report
            await original.settle_boundary(seed, app_user_sessionmaker, tmp_path, run_id)


@pytest.mark.parametrize('fault', ['changed', 'foreign', 'alias', 'credential', 'untracked', 'snapshot', 'revoked'])
async def test_remote_files_fail_before_http_without_replacing_truth_and_keep_recovery(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path, fault
):
    from app.api import chat_sessions, upload
    from app.models.user import User
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.content_input_receipts import latest_workspace_receipt

    seed, api, session_id = remote_work
    for router in (chat_sessions.router, upload.router):
        api._transport.app.include_router(router)
    await original.prepare_employee(seed, app_user_sessionmaker, monkeypatch, tmp_path)
    monkeypatch.setattr(upload, 'WORKSPACE_ROOT', tmp_path)
    body = b'synthetic-remote-credential' if fault == 'credential' else b'ORIGINAL_FILE_BYTES\x00\xff'
    uploaded = await api.post('/chat/upload', files={'file': ('bound.bin', body, 'application/octet-stream')},
        data={'agent_id': str(seed['agent']), 'skip_personal_kb': 'true'})
    assert uploaded.status_code == 200, uploaded.text
    path = uploaded.json()['workspace_path']
    root = tmp_path / str(seed['agent'])
    base = f"/agents/{seed['agent']}/sessions/{session_id}"
    with remote_server() as remote:
        config = await original.original.configure(seed, api, remote)
        assert (await api.post(config + '/runtime', json=selection(seed))).status_code == 200
        remote.reply = lambda request: {'task': {'id': request['id'], 'contextId': 'file-context',
            'status': {'state': 'TASK_STATE_COMPLETED', 'message': {'messageId': request['id'],
                'role': 'ROLE_AGENT', 'parts': [{'text': 'File consumed.'}]}}}}
        async def send(attachments):
            response = await api.post(base + '/runs', json={'content': 'Read the selected file.',
                'input_id': str(uuid4()), 'idempotency_key': str(uuid4()), 'attachments': attachments})
            assert response.status_code == 201, response.text
            run_id = response.json()['run_id']
            await original.execute_chat(seed, run_id, app_user_sessionmaker)
            return run_id
        captured = fault in {'snapshot', 'revoked'}
        if captured:
            first = await send([{'path': path}])
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                assert (await db.get(RuntimeTask, UUID(first))).status == 'completed'
            await original.settle_boundary(seed, app_user_sessionmaker, tmp_path, first)
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            manifest = await db.scalar(select(WorkspaceResourceManifest).where(
                WorkspaceResourceManifest.agent_id == seed['agent'], WorkspaceResourceManifest.path == path))
            receipt = await latest_workspace_receipt(db, seed['tenant'], seed['agent'], path)
            if fault == 'foreign':
                user_id = uuid4()
                db.add(User(id=user_id, tenant_id=seed['tenant'], role='member', username=user_id.hex,
                    email=user_id.hex + '@example.test', password_hash='synthetic', display_name='Other file owner'))
                await db.flush()
                manifest.owner_user_id = user_id
            elif fault == 'untracked':
                await db.delete(receipt)
            elif fault == 'revoked':
                manifest.authority_state = 'quarantined'
            elif fault == 'snapshot':
                snapshot = root / f'runtime_artifacts/workspace_content_versions/{receipt.id}/files/uploads/bound.bin'
                snapshot.write_bytes(b'Changed saved version')
            await db.commit()
        if fault == 'changed':
            (root / path).write_bytes(b'Changed outside the verified writer')
        elif fault == 'alias':
            replacement = tmp_path / 'aliased.bin'
            replacement.write_bytes(body)
            (root / path).unlink()
            (root / path).symlink_to(replacement)
        denied_run = await send([] if captured else [{'path': path}])
        assert len([request for request in remote.requests if request[0] == 'POST']) == int(captured)
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, UUID(denied_run))
            state = task.metadata_json['remote_a2a']
            assert task.status == 'needs_reconciliation' and state['operations'][-1]['phase'] == 'not_started'
            assert state['source_error']['code']
        view = await api.get(config + f'/tasks/{denied_run}')
        assert view.status_code == 200 and 'abandon' in view.json()['recovery_actions'], view.text
        if fault == 'snapshot':
            assert snapshot.read_bytes() == b'Changed saved version'
        if fault == 'revoked':
            await original.settle_boundary(seed, app_user_sessionmaker, tmp_path, denied_run)
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                current = await db.get(WorkspaceResourceManifest, manifest.id)
                current.authority_state = 'owned'
                await db.commit()
            abandoned = await api.post(config + f'/tasks/{denied_run}/operations', json={
                'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant']),
                'expected_version': 1, 'operation_id': str(uuid4()), 'action': 'abandon', 'params': {}})
            assert abandoned.status_code == 200, abandoned.text
            recovered = await send([])
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                assert (await db.get(RuntimeTask, UUID(recovered))).status == 'completed'
            assert len([request for request in remote.requests if request[0] == 'POST']) == 2
