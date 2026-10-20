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


@pytest.mark.parametrize(('strict', 'flow'), [(False, 'completed'), (True, 'completed'),
    (True, 'input_required'), (True, 'unknown'), (True, 'revoked'), (True, 'installation'),
    (True, 'origin_tamper'), (True, 'delivery_rebound')])
async def test_verified_feishu_runs_selected_remote_with_real_principal_and_original_delivery(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path, strict, flow
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
    async def profile(_agent, *, sender_open_id, **_kwargs):
        return {'open_id': sender_open_id, 'name': 'Verified channel requester'}
    async def send(_self, app_id, _secret, receive_id, msg_type, content, **kwargs):
        sent.append({'app_id': app_id, 'target': receive_id, 'type': msg_type,
            'content': content, 'id_type': kwargs.get('receive_id_type')})
        return {'data': {'message_id': 'synthetic-' + str(len(sent))}}
    async def send_file(_self, app_id, _secret, receive_id, path, **_kwargs):
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
            app_id='cli_remote_work', app_secret='synthetic-company-secret', verification_token='synthetic-callback')
        provider = IdentityProvider(id=uuid4(), tenant_id=seed['tenant'], provider_type='feishu',
            name='Verified company identity', is_active=True, config={'app_id': config.app_id})
        db.add_all([config, provider])
        await db.flush()
        db.add_all([ExternalIdentity(id=uuid4(), provider_id=provider.id, user_id=seed['owner'],
            provider_open_id='ou_remote_owner', provider_user_id='remote-member'),
            UserAssistantBinding(tenant_id=seed['tenant'], user_id=seed['owner'], agent_id=seed['agent'],
                status='active', source='explicit_selection', requester_user_id=seed['owner'],
                owner_user_id_at_bind=seed['owner'])])
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
        async def accept(key, text):
            body = {'header': {'event_id': key, 'event_type': 'im.message.receive_v1', 'token': 'synthetic-callback'},
                'event': {'sender': {'sender_id': {'open_id': 'ou_remote_owner', 'user_id': 'remote-member'}},
                    'message': {'message_id': key, 'message_type': 'text', 'chat_type': 'p2p', 'chat_id': 'oc_remote',
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
                assert event.result_runtime_task_id is not None, event.processing_receipt_json
                return event
        event = await accept('remote-channel-first', 'Complete this original channel assignment.')
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
        await original.execute_chat(seed, str(run_id), app_user_sessionmaker)
        if flow in {'revoked', 'installation', 'origin_tamper'}:
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
            assert task.status == ('needs_reconciliation' if flow == 'unknown' else 'completed'), task.metadata_json
            assert task.root_user_id == seed['owner'] and task.root_external_principal_id == principal_id
            assert task.metadata_json['remote_a2a']['channel_origin']['ingress_event_id'] == str(event.id)
            assert task.metadata_json['budget_interactive'] is False
        report = (receiver.directory / ('report-1.txt' if flow == 'input_required' else 'report-0.txt')).read_text()
        notifications = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='channel-result')
        assert notifications['delivered'] >= 1, notifications
        if flow == 'delivery_rebound':
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                user_id = uuid4()
                db.add(User(id=user_id, tenant_id=seed['tenant'], username=user_id.hex, email=user_id.hex + '@example.test',
                    password_hash='synthetic', display_name='New channel account', role='member'))
                await db.flush()
                (await db.get(ExternalPrincipal, principal_id)).linked_user_id = user_id
                await db.commit()
            before = len(sent), len(returned_files)
        delivery = await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='channel-return')
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            outbox = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == run_id)
                .order_by(ChannelDeliveryOutbox.created_at.desc()).limit(1))
            assert outbox is not None, delivery
            assert outbox.channel_config_id == config.id and outbox.session_id == session_id
            assert outbox.user_id == seed['owner'] and outbox.external_principal_id == principal_id
            if flow == 'delivery_rebound':
                artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == run_id))
                artifact_id = artifact.id
            else:
                assert outbox.status == 'delivered', (delivery, outbox.last_error)
        if flow == 'delivery_rebound':
            assert (len(sent), len(returned_files)) == before
            # Transport revocation cannot take the original owner's local work away.
            local = await api.get(f"/agents/{seed['agent']}/files/artifacts/{artifact_id}/content")
            assert local.status_code == 200 and local.json()['content'] == report, local.text
            return
        if flow == 'unknown':
            texts = [json.loads(item['content']).get('text', '') for item in sent]
            assert not returned_files and all(report not in text for text in texts)
            assert '远端运行需要对账。请在 Hive 原工作中查看状态，再选择继续、取消或放弃。' in texts
            assert outbox.terminal_status == 'needs_reconciliation' and outbox.metadata_json['remote_recovery_notice'] is True
            assert len(list(receiver.directory.glob('consumed-*.json'))) == 1
            return
        assert any(report in json.loads(item['content']).get('text', '') for item in sent)
        assert (config.app_id, 'ou_remote_owner', report.encode()) in returned_files
        assert all(item['app_id'] == config.app_id and item['target'] == 'ou_remote_owner' for item in sent)
        before = len(sent), len(returned_files)
        await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='channel-result-replay')
        await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='channel-return-replay')
        assert (len(sent), len(returned_files)) == before
        await original.settle_boundary(seed, app_user_sessionmaker, tmp_path, str(run_id))
