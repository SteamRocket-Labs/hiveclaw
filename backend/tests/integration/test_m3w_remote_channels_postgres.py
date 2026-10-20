"""Verified tenant Feishu input runs the selected remote and returns on the same bot."""

import json
from uuid import uuid4
from copy import deepcopy

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.database import get_db, tenant_scoped_session
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.external_principal import ExternalPrincipal
from app.models.runtime_task import RuntimeTask
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user_assistant_binding import UserAssistantBinding
from tests.integration import test_m3t_remote_chat_postgres as original
from tests.integration.test_m3q_remote_runtime_postgres import selection
from tests.integration.test_m3u_remote_sdk_context_postgres import sdk_receiver

app_user_engine = original.app_user_engine
app_user_sessionmaker = original.app_user_sessionmaker
machine_api = original.machine_api
migrated_pg_url = original.migrated_pg_url
owner_engine = original.owner_engine
owner_sessionmaker = original.owner_sessionmaker
pg_container = original.pg_container
remote_work = original.remote_work
pytestmark = pytest.mark.usefixtures('migrated_pg_url')


def _texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _texts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _texts(child)


@pytest.mark.parametrize(('strict', 'flow', 'shared'),
    [(False, 'completed', False), (True, 'completed', False), (True, 'input_required', False),
        (True, 'unknown', False), (True, 'revoked', False), (True, 'installation', False),
        (True, 'origin_tamper', False), (True, 'delivery_rebound', False), (True, 'source_changed', False),
        (True, 'notice_tamper', False), (True, 'notice_stale', False), (True, 'no_run', False),
        (True, 'no_run_revoked', False)] + [(True, flow, True) for flow in (
            'completed', 'web', 'private_context', 'input_required', 'unknown', 'source_changed', 'member_left', 'delivery_audience_changed')])
async def test_verified_feishu_runs_selected_remote_with_real_principal_and_original_delivery(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path, strict, flow, shared
):
    from app.api import chat_sessions, feishu, files, tenant_channels
    from app.models.chat_artifact import ChatArtifact
    from app.models.user import User
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.channel_ingress_inbox import ChannelIngressInboxService
    from app.services.feishu_service import FeishuService
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

    seed, api, _session_id = remote_work
    for router in (chat_sessions.router, files.router):
        api._transport.app.include_router(router)
    await original.prepare_employee(seed, app_user_sessionmaker, monkeypatch, tmp_path)
    sent, returned_files = [], []
    members = {'ou_remote_owner', 'ou_remote_member'}
    async def membership(*_args, **_kwargs):
        return set(members)
    monkeypatch.setattr(FeishuService, 'get_chat_member_open_ids', membership)
    async def profile(_agent, *, sender_open_id, **_kwargs):
        return {'open_id': sender_open_id, 'name': 'Verified channel requester'}
    async def send(_self, app_id, _secret, receive_id, msg_type, content, **kwargs):
        if app_id != config.app_id:
            return {'data': {'message_id': 'other-installation-' + uuid4().hex}}
        sent.append({'app_id': app_id, 'target': receive_id, 'type': msg_type,
            'content': content, 'id_type': kwargs.get('receive_id_type')})
        return {'data': {'message_id': 'synthetic-' + str(len(sent))}}
    async def send_file(_self, app_id, _secret, receive_id, path, **_kwargs):
        if app_id != config.app_id:
            return {'data': {'message_id': 'other-installation-file-' + uuid4().hex}}
        returned_files.append((app_id, receive_id, path.read_bytes()))
        return {'data': {'message_id': 'synthetic-file-' + str(len(returned_files))}}
    async def no_card(*_args, **_kwargs):
        return None
    monkeypatch.setattr(feishu, '_resolve_feishu_sender_profile', profile)
    monkeypatch.setattr(FeishuService, 'send_message', send)
    monkeypatch.setattr(FeishuService, 'upload_and_send_file', send_file)
    for name in ('create_card_entity', 'send_card_by_card_id', 'patch_message', 'set_card_streaming_mode'):
        monkeypatch.setattr(feishu.feishu_service, name, no_card)
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        config = TenantChannelConfig(id=uuid4(), tenant_id=seed['tenant'], channel_type='feishu',
            app_id='cli_remote_' + seed['agent'].hex, app_secret='synthetic-company-secret', verification_token='synthetic-callback')
        provider = IdentityProvider(id=uuid4(), tenant_id=seed['tenant'], provider_type='feishu',
            name='Verified company identity', is_active=True, config={'app_id': config.app_id})
        db.add_all([config, provider])
        await db.flush()
        db.add_all([ExternalIdentity(id=uuid4(), provider_id=provider.id, user_id=seed['owner'],
            provider_open_id='ou_remote_owner', provider_user_id='remote-member'),
            UserAssistantBinding(tenant_id=seed['tenant'], user_id=seed['owner'], agent_id=seed['agent'],
                status='active', source='explicit_selection', requester_user_id=seed['owner'],
                owner_user_id_at_bind=seed['owner'])])
        if shared:
            from app.models.audit import ChatMessage
            from app.models.security_audit import ResourcePermission
            from app.services.channel_ingress_inbox import channel_installation_ref
            member = User(id=uuid4(), tenant_id=seed['tenant'], username=uuid4().hex,
                email=uuid4().hex + '@example.test', password_hash='synthetic', role='member', display_name='Group member')
            db.add(member)
            await db.flush()
            db.add_all([ExternalIdentity(id=uuid4(), provider_id=provider.id, user_id=member.id,
                provider_open_id='ou_remote_member', provider_user_id='remote-other'),
                ResourcePermission(id=uuid4(), tenant_id=seed['tenant'], principal_type='user', principal_id=member.id,
                    resource_type='agent', resource_id=seed['agent'], actions=['read', 'execute'], effect='allow'),
                ChatMessage(id=uuid4(), tenant_id=seed['tenant'], agent_id=seed['agent'], user_id=seed['owner'],
                    role='user', content='PRIVATE_REMOTE_CHANNEL_CANARY', conversation_id=str(_session_id))])
            config.extra_config = {'shared_group_entry': {'app_id': config.app_id,
                'installation_ref': channel_installation_ref(config, fallback=''),
                'routes': [{'chat_id': 'oc_remote', 'agent_id': str(seed['agent'])}]}}
            from datetime import datetime, timezone
            from app.services.external_principal_service import resolve_or_create_external_principal
            await db.flush()
            linked = await resolve_or_create_external_principal(db, tenant_id=seed['tenant'], provider='feishu',
                installation_ref=channel_installation_ref(config, fallback=''), tenant_channel_config_id=config.id,
                subject_id='ou_remote_member', display_name='Group member')
            linked.principal.linked_user_id = member.id
            linked.principal.binding_method = 'feishu_tenant_identity'
            linked.principal.binding_verified_at = linked.principal.linked_at = datetime.now(timezone.utc)
        if shared and flow == 'private_context':
            from app.models.chat_session import ChatSession
            from app.models.session_v2 import SessionCommand
            from app.services.session_v2_persistence import resolve_session_mutation_authority, accept_human_input
            from app.services import memory_service
            authority = await resolve_session_mutation_authority(db, user=await db.get(User, seed['owner']),
                agent_id=seed['agent'], session_id=_session_id, action='mutate_session_input')
            accepted = await accept_human_input(db, authority=authority, intent={'kind': 'start_turn',
                'session_id': str(_session_id), 'input_id': str(uuid4()), 'idempotency_key': 'private-memory-source',
                'content_parts': [{'type': 'text', 'text': 'PRIVATE_REMOTE_MEMORY_CANARY'}]})
            command = await db.get(SessionCommand, accepted.command_id)
            private_source = {'source_kind': 'session_command', 'source_id': str(command.id), 'source_version': command.request_hash}
            async def memory(*_args, **kwargs):
                owner = kwargs['current_user_id'] == seed['owner']
                return memory_service.MemoryContextResult(content='PRIVATE_REMOTE_MEMORY_CANARY' if owner else '',
                    status='ready', code='memory_context_ready', user_message='Controlled scoped memory',
                    source_refs=(private_source,) if owner else (), source_unbound=())
            monkeypatch.setattr(memory_service, 'build_memory_context', memory)
        await db.commit()
    web = FastAPI()
    web.include_router(tenant_channels.router, prefix='/api')
    async def database():
        async with app_user_sessionmaker() as db:
            yield db
            await db.commit()
    web.dependency_overrides[get_db] = database
    async with sdk_receiver(tmp_path / 'receiver', strict, input_required=flow == 'input_required') as receiver:
        path = await original.original.configure(seed, api, receiver)
        assert (await api.post(path + '/runtime', json=selection(seed))).status_code == 200
        if flow in {'no_run', 'no_run_revoked'}:
            from app.services import remote_a2a_chat
            from fastapi import HTTPException
            async def denied_admission(db, **_kwargs):
                if flow == 'no_run_revoked':
                    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as changed:
                        principal = await changed.scalar(select(ExternalPrincipal).where(
                            ExternalPrincipal.tenant_id == seed['tenant']))
                        principal.status = 'revoked'
                        await changed.commit()
                raise HTTPException(409, detail={'code': 'controlled_admission_unavailable'})
            monkeypatch.setattr(remote_a2a_chat, 'capture_executor', denied_admission)
        async def accept(key, text, *, subject="ou_remote_owner"):
            body = {'header': {'event_id': key, 'event_type': 'im.message.receive_v1', 'token': 'synthetic-callback'},
                'event': {'sender': {'sender_id': {'open_id': subject, 'user_id': 'remote-member' if subject == 'ou_remote_owner' else 'remote-other'}},
                    'message': {'message_id': key, 'message_type': 'text', 'chat_type': 'group' if shared else 'p2p', 'chat_id': 'oc_remote',
                        'content': json.dumps({'text': text})}}}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url='http://test') as client:
                for _replay in range(2):
                    accepted = await client.post(f"/api/channel/feishu/tenant/{seed['tenant']}/webhook", json=body)
                    assert accepted.status_code == 200 and accepted.json()['routed'], accepted.text
            inbox = await ChannelIngressInboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='remote-channel')
            assert inbox['processed'] == 1, inbox
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                event = await db.scalar(select(ChannelIngressEvent).where(ChannelIngressEvent.tenant_id == seed['tenant'],
                    ChannelIngressEvent.provider_event_id == key))
                if flow not in {'no_run', 'no_run_revoked'}:
                    assert event.result_runtime_task_id is not None, event.processing_receipt_json
                return event
        event = await accept('remote-channel-first', 'Complete this original channel assignment.')
        if flow in {'no_run', 'no_run_revoked'}:
            assert event.result_runtime_task_id is None
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                assert await db.scalar(select(RuntimeTask.id).where(RuntimeTask.parent_agent_id == seed['agent'])) is None
                principal = await db.get(ExternalPrincipal, event.external_principal_id)
                assert principal.status == ('revoked' if flow == 'no_run_revoked' else 'active')
            assert not (receiver.directory / 'consumed-0.json').exists() and not returned_files
            texts = [json.loads(item['content']).get('text', '') for item in sent]
            warning = '消息已记录，尚未确认启动任务。请在原工作中查看处理状态；需要修复时继续处理这条消息。'
            assert texts == ([warning] if flow == 'no_run' else []), texts
            return
        run_id, session_id, principal_id = event.result_runtime_task_id, event.result_session_id, event.external_principal_id
        if flow == 'unknown':
            from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError
            rpc = RemoteA2AEndpoint.rpc
            async def lose_response(endpoint, *args, **kwargs):
                await rpc(endpoint, *args, **kwargs)
                raise RemoteA2AError('remote_a2a_response_lost', execution_status='unknown')
            monkeypatch.setattr(RemoteA2AEndpoint, 'rpc', lose_response)
        if flow in {'revoked', 'installation', 'origin_tamper'}:
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                if flow == 'revoked':
                    (await db.get(ExternalPrincipal, principal_id)).status = 'revoked'
                elif flow == 'installation':
                    (await db.get(TenantChannelConfig, config.id)).app_id = 'cli_replaced'
                else:
                    task = await db.get(RuntimeTask, run_id)
                    changed = deepcopy(task.metadata_json)
                    changed['remote_a2a']['channel_origin']['ingress_event_id'] = str(uuid4())
                    task.metadata_json = changed
                await db.commit()
        if flow == 'member_left':
            members.remove('ou_remote_owner')
        if flow in {'source_changed', 'notice_tamper', 'notice_stale'}:
            (tmp_path / str(seed['agent']) / 'soul.md').write_text('Protected source replacement')
        await original.execute_chat(seed, str(run_id), app_user_sessionmaker)
        if flow in {'revoked', 'installation', 'origin_tamper', 'member_left'}:
            assert not (receiver.directory / 'consumed-0.json').exists()
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                task = await db.get(RuntimeTask, run_id)
                assert task.status == 'needs_reconciliation', task.metadata_json
                assert task.metadata_json['remote_a2a']['operations'][-1]['phase'] == 'not_started'
            return
        if flow == 'input_required':
            await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='channel-question')
            await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='channel-question')
            assert any('Please provide the requested detail' in item['content'] for item in sent)
            next_event = await accept('remote-channel-reply', 'Here is the requested detail.')
            assert next_event.result_session_id == session_id and next_event.result_runtime_task_id == run_id
            await original.execute_chat(seed, str(run_id), app_user_sessionmaker)
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, run_id)
            assert task.status == ('needs_reconciliation' if flow in {'unknown', 'source_changed', 'notice_tamper', 'notice_stale'} else 'completed'), task.metadata_json
            assert task.root_user_id == seed['owner'] and task.root_external_principal_id == principal_id
            assert task.metadata_json['remote_a2a']['channel_origin']['ingress_event_id'] == str(event.id)
            assert task.metadata_json['budget_interactive'] is False
        if flow == 'input_required':
            followup = json.loads((receiver.directory / 'consumed-1.json').read_text())
            text = '\n'.join(_texts(followup['context']['messages']))
            assert 'Complete this original channel assignment.' in text
            assert 'Please provide the requested detail' in text
        report = '' if flow in {'source_changed', 'notice_tamper', 'notice_stale'} else (receiver.directory / ('report-1.txt' if flow == 'input_required' else 'report-0.txt')).read_text()
        notifications = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='channel-result')
        assert notifications['delivered'] >= 1, notifications
        if flow == 'notice_stale':
            abandoned = await api.post(path + f'/tasks/{run_id}/operations', json={
                'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant']),
                'expected_version': 1, 'operation_id': str(uuid4()), 'action': 'abandon', 'params': {}})
            assert abandoned.status_code == 200 and abandoned.json()['local_abandoned'], abandoned.text
        if flow == 'notice_tamper':
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                pending = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == run_id))
                pending.text_content = 'Protected source replacement'
                await db.commit()
        if flow == 'delivery_rebound':
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                user_id = uuid4()
                db.add(User(id=user_id, tenant_id=seed['tenant'], username=user_id.hex, email=user_id.hex + '@example.test',
                    password_hash='synthetic', display_name='New channel account', role='member'))
                await db.flush()
                (await db.get(ExternalPrincipal, principal_id)).linked_user_id = user_id
                await db.commit()
            before = len(sent), len(returned_files)
        if flow == 'delivery_audience_changed':
            members.add('ou_unresolved_recipient')
        before_delivery = len(sent), len(returned_files)
        delivery = await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='channel-return')
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            outbox = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == run_id)
                .order_by(ChannelDeliveryOutbox.created_at.desc()).limit(1))
            assert outbox is not None, delivery
            assert outbox.channel_config_id == config.id and outbox.session_id == session_id
            assert outbox.user_id == seed['owner'] and outbox.external_principal_id == (None if shared else principal_id)
            if flow == 'delivery_audience_changed':
                assert outbox.status == 'pending', (delivery, outbox.last_error)
            elif flow in {'notice_tamper', 'notice_stale', 'private_context'}:
                assert outbox.status == 'dead_letter', (delivery, outbox.last_error)
            elif flow == 'delivery_rebound':
                artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == run_id))
                artifact_id = artifact.id
            else:
                assert outbox.status == 'delivered', (delivery, outbox.last_error)
        if flow == 'delivery_audience_changed':
            assert (len(sent), len(returned_files)) == before_delivery
            return
        if flow == 'delivery_rebound':
            assert (len(sent), len(returned_files)) == before
            # Transport revocation cannot take the original owner's local work away.
            local = await api.get(f"/agents/{seed['agent']}/files/artifacts/{artifact_id}/content")
            assert local.status_code == 200 and local.json()['content'] == report, local.text
            return
        if flow in {'source_changed', 'notice_tamper', 'notice_stale'}:
            assert not (receiver.directory / 'consumed-0.json').exists() and not returned_files
            texts = [json.loads(item['content']).get('text', '') for item in sent]
            assert 'Protected source replacement' not in texts
            if flow == 'source_changed':
                assert '远端任务尚未启动。请在 Hive 原工作中查看状态和可用的恢复操作。' in texts
                assert outbox.metadata_json['remote_recovery_notice'] is True
            else:
                assert (len(sent), len(returned_files)) == before_delivery
            before_replay = len(sent), len(returned_files)
            await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='notice-replay')
            assert (len(sent), len(returned_files)) == before_replay
            # A status notice never grants permission to read the protected packet.
            from app.services.remote_a2a_artifacts import load_packet
            from app.services.content_input_receipts import ContentInputReceiptError
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                with pytest.raises(ContentInputReceiptError):
                    await load_packet(db, seed['tenant'], outbox.metadata_json['remote_result'],
                        user=await db.get(User, seed['owner']), allow_unconfirmed=True)
            return
        if flow == 'unknown':
            texts = [json.loads(item['content']).get('text', '') for item in sent]
            assert not returned_files and all(report not in text for text in texts)
            assert '远端运行结果尚未确认。请在 Hive 原工作中查看状态和可用的恢复操作。' in texts
            assert outbox.terminal_status == 'needs_reconciliation' and outbox.metadata_json['remote_recovery_notice'] is True
            assert len(list(receiver.directory.glob('consumed-*.json'))) == 1
            if shared:
                from app.core.security import create_access_token
                own = await api.get(path + f'/tasks/{run_id}')
                assert own.status_code == 200 and own.json()['status'] == 'needs_reconciliation', own.text
                other = await api.get(path + f'/tasks/{run_id}', headers={'Authorization': 'Bearer ' +
                    create_access_token(str(member.id), 'member', str(seed['tenant']))})
                assert other.status_code == 404, other.text
                abandoned = await api.post(path + f'/tasks/{run_id}/operations', json={
                    'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant']),
                    'expected_version': 1, 'operation_id': str(uuid4()), 'action': 'abandon', 'params': {}})
                assert abandoned.status_code == 200 and abandoned.json()['local_abandoned'], abandoned.text
                assert len(list(receiver.directory.glob('consumed-*.json'))) == 1
            return
        if flow == 'private_context':
            assert not returned_files and all(report not in json.loads(item['content']).get('text', '') for item in sent)
        else:
            assert any(report in json.loads(item['content']).get('text', '') for item in sent)
        target = 'oc_remote' if shared else 'ou_remote_owner'
        if flow != 'private_context':
            assert (config.app_id, target, report.encode()) in returned_files
        assert all(item['app_id'] == config.app_id and item['target'] == target for item in sent)
        before = len(sent), len(returned_files)
        await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='channel-result-replay')
        await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='channel-return-replay')
        assert (len(sent), len(returned_files)) == before
        await original.settle_boundary(seed, app_user_sessionmaker, tmp_path, str(run_id))
        if shared and flow in {'completed', 'web', 'private_context'}:
            from app.core.security import create_access_token
            from app.models.chat_session import ChatSession
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                session = await db.get(ChatSession, session_id)
                artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == run_id))
                assert session.user_id is None and session.external_principal_id is None and session.audience_kind == 'group'
            token = create_access_token(str(member.id), 'member', str(seed['tenant']))
            read = await api.get(f"/agents/{seed['agent']}/files/artifacts/{artifact.id}/content",
                headers={'Authorization': 'Bearer ' + token})
            if flow == 'private_context':
                assert read.status_code == 403 and 'PRIVATE_REMOTE_MEMORY_CANARY' not in read.text, read.text
            else:
                assert read.status_code == 200 and read.json()['content'] == report, read.text
            history = await api.get(f"/agents/{seed['agent']}/sessions/{session_id}/messages",
                headers={'Authorization': 'Bearer ' + token})
            assert history.status_code == 200 and 'PRIVATE_REMOTE_CHANNEL_CANARY' not in history.text, history.text
            if flow == 'private_context':
                assert 'PRIVATE_REMOTE_MEMORY_CANARY' not in history.text and report not in set(_texts(history.json()))
            else:
                assert report in set(_texts(history.json())), history.text
            if flow == 'web':
                reply = await api.post(f"/agents/{seed['agent']}/sessions/{session_id}/runs",
                    headers={'Authorization': 'Bearer ' + token}, json={'content': 'Continue the same shared work from Web.',
                        'input_id': str(uuid4()), 'idempotency_key': 'shared-web-member'})
                assert reply.status_code == 201 and reply.json().get('run_id'), reply.text
                second_id = reply.json()['run_id']
            else:
                second = await accept('remote-group-member', 'Continue the same shared work.', subject='ou_remote_member')
                assert second.result_session_id == session_id and second.result_runtime_task_id != run_id
                second_id = str(second.result_runtime_task_id)
            await original.execute_chat(seed, second_id, app_user_sessionmaker)
            consumed = json.loads((receiver.directory / 'consumed-1.json').read_text())
            assert consumed['context']['principal']['requester_user_id'] == str(member.id)
            assert 'PRIVATE_REMOTE_CHANNEL_CANARY' not in consumed['sdk_input']
            if flow == 'private_context':
                first = json.loads((receiver.directory / 'consumed-0.json').read_text())
                assert 'PRIVATE_REMOTE_MEMORY_CANARY' in first['sdk_input']
                assert first['context_id'] != consumed['context_id'] and 'PRIVATE_REMOTE_MEMORY_CANARY' not in consumed['sdk_input']
                assert consumed['context']['history_receipt']['status'] == 'degraded'
            else:
                assert report in set(_texts(consumed['context']['messages']))
            await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='group-member-result')
            delivered = await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='group-member-return')
            assert delivered['delivered'] >= 1, delivered
