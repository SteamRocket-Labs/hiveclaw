"""The actual SDK text entry consumes authorized context and returns original work."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.database import tenant_scoped_session
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.chat_artifact import ChatArtifact
from app.models.session_v2 import SessionModelResult
from tests.integration import test_m3t_remote_chat_postgres as original
from tests.integration.test_m3q_remote_runtime_postgres import selection

app_user_engine = original.app_user_engine
app_user_sessionmaker = original.app_user_sessionmaker
machine_api = original.machine_api
migrated_pg_url = original.migrated_pg_url
owner_engine = original.owner_engine
owner_sessionmaker = original.owner_sessionmaker
pg_container = original.pg_container
remote_work = original.remote_work
pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@asynccontextmanager
async def sdk_receiver(directory, strict, *, input_required=False):
    directory.mkdir()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    env = {key: os.environ[key] for key in ['PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'PYTHONPATH'] if key in os.environ}
    script = Path(__file__).parents[1] / 'fixtures/remote_chat_sdk_receiver.py'
    log = (directory / 'server.log').open('wb')
    process = await asyncio.create_subprocess_exec(sys.executable, '-B', str(script), '--fd', str(listener.fileno()),
        '--directory', str(directory), *(['--strict'] if strict else []),
        *(['--input-required'] if input_required else []), pass_fds=(listener.fileno(),),
        env=env, stdout=asyncio.subprocess.PIPE, stderr=log)
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        assert line, (directory / 'server.log').read_text()
        ready = json.loads(line)
        assert ready == {'ready': True, 'pid': process.pid}
        yield SimpleNamespace(origin=f'http://127.0.0.1:{listener.getsockname()[1]}', pid=process.pid, directory=directory)
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        listener.close()
        log.close()


@pytest.mark.parametrize('strict', [False, True])
async def test_sdk_receiver_consumes_full_context_and_prior_work_without_a_native_model(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path, strict
):
    from app.api import chat_sessions, files
    from app.memory.t0.ledger import replay_t0_session_events
    from app.services.agent_context import load_agent_context_resource
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

    seed, api, session_id = remote_work
    api._transport.app.include_router(chat_sessions.router)
    api._transport.app.include_router(files.router)
    await original.prepare_employee(seed, app_user_sessionmaker, monkeypatch, tmp_path,
        personality='ORIGINAL_REMOTE_EMPLOYEE_CONTEXT\n' + 'AUTHORIZED_CONTEXT_LINE\n' * 2000 + '完整授权背景\n' + 'DECISIVE_CONTEXT_TAIL')
    source = await load_agent_context_resource(agent_id=seed['agent'], tenant_id=seed['tenant'], resource_ref='soul')
    base = f"/agents/{seed['agent']}/sessions/{session_id}"
    async with sdk_receiver(tmp_path / 'receiver', strict) as receiver:
        path = await original.original.configure(seed, api, receiver)
        assert (await api.post(path + '/runtime', json=selection(seed))).status_code == 200
        previous = None
        for turn in range(2):
            body = {'content': f'Original task {turn}\nContinue using the complete authorized work.',
                'input_id': str(uuid4()), 'idempotency_key': f'sdk-context-{turn}'}
            response = await api.post(base + '/runs', json=body)
            assert response.status_code == 201, response.text
            run_id = response.json()['run_id']
            await original.execute_chat(seed, run_id, app_user_sessionmaker)
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                task = await db.get(RuntimeTask, UUID(run_id))
                assert task.status == 'completed', task.metadata_json
                assert await db.scalar(select(func.count()).select_from(SessionModelResult).where(
                    SessionModelResult.run_id == task.id)) == 0
                packet = task.metadata_json['remote_a2a']['operations'][-1]['params']['message']
            consumed = json.loads((receiver.directory / f'consumed-{turn}.json').read_text())
            report = (receiver.directory / f'report-{turn}.txt').read_text()
            assert consumed['pid'] == receiver.pid != os.getpid()
            assert packet['parts'][0]['text'] == body['content']
            assert consumed['sdk_input'] == '\n'.join(part['text'] for part in packet['parts'])
            assert 'DECISIVE_CONTEXT_TAIL' in consumed['context']['resources']['soul']
            assert consumed['context']['resources']['soul'] == source.content
            assert len(consumed['context']['resources']['soul']) > 32_000
            assert json.loads(report)['instruction'] == body['content']
            assert json.loads(report)['context_sha256'] == hashlib.sha256(packet['parts'][1]['text'].encode()).hexdigest()
            if previous is not None:
                assert any(message.get('content') == previous for message in consumed['context']['messages'])
            delivered = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='sdk-return')
            assert delivered['delivered'] >= 1, delivered
            events = (await api.get(base + '/transcript')).json()
            assert any(event.get('content') == report for event in events)
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                assert await db.scalar(select(ChatTranscriptEvent.content).where(ChatTranscriptEvent.run_id == UUID(run_id),
                    ChatTranscriptEvent.event_type == 'assistant_message')) == report
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == UUID(run_id),
                    ChatArtifact.session_id == session_id))
                assert artifact is not None
                assert artifact.snapshot_json['content_hash'] == hashlib.sha256(report.encode()).hexdigest()
                artifact_id = artifact.id
            content = await api.get(f"/agents/{seed['agent']}/files/artifacts/{artifact_id}/content")
            assert content.status_code == 200 and content.json()['content'] == report, content.text[:300]
            previous = report
            await original.settle_boundary(seed, app_user_sessionmaker, tmp_path, run_id)
            # The immutable artifact event still belongs to its source run;
            # late session delivery cannot reopen that run's sealed segment.
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                event_id = await db.scalar(select(ChatTranscriptEvent.id).where(
                    ChatTranscriptEvent.run_id == UUID(run_id),
                    ChatTranscriptEvent.event_type == 'remote_a2a_artifacts'))
            assert event_id is not None
            assert await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)
            delivery = next(event for event in replay_t0_session_events(agent_id=seed['agent'],
                session_id=session_id, data_root=tmp_path)
                if event.metadata.get('transcript_event_id') == str(event_id))
            assert delivery.runtime_task_id is None and delivery.turn_id is None
            assert UUID(delivery.metadata['t0_bridge_joined_segment_run_id']) == UUID(run_id)
