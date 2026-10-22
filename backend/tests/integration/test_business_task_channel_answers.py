"""A quoted Feishu question returns an answer to its exact native business run."""

import base64
import copy
import hashlib
import json
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionTurnInput
from tests.integration.test_business_task_text_sources import (
    original_api, provider, delivered, s3_server,  # noqa: F401
    test_business_task_text_rechecks_its_actual_original_source as _business_source,
)

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('provider', ['filesystem'], indirect=True)
@pytest.mark.parametrize('state', ['answer', 'withdrawn', 'cancelled', 'wrong_sender', 'ordinary', 'control_text', 'image', 'vision_image', 'image_unavailable', 'unknown_receipt', 'application_changed'])
async def test_feishu_quoted_business_answer_survives_ingress_replay(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, state,  # noqa: F811
):
    async def inspect(**context):
        seed, factory, model = context['seed'], context['db_factory'], context['model_client']
        run_id = context['run_id']
        from app.api import feishu
        from app.models.audit import ChatMessage
        from app.models.channel_config import ChannelConfig
        from app.models.channel_ingress_event import ChannelIngressEvent
        from app.models.chat_session import ChatSession
        from app.models.user import User
        from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
        from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
        from app.services.channel_ingress_inbox import (
            ChannelIngressSubmission, ChannelIngressInboxService, enqueue_channel_ingress_event,
        )
        from app.services.feishu_service import FeishuService
        from app.services.channel_provider_receipts import feishu_message_scope

        texts = []
        subject = 'ou-business-other' if state == 'wrong_sender' else 'ou-business-owner'
        answer_text = '/permissions full' if state == 'control_text' else 'Use a report for this original business work.'
        image_bytes = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8S8AAAAASUVORK5CYII=')

        async def send(self, _app, _secret, recipient, _kind, content, **_kwargs):
            texts.append((recipient, content))
            if state == 'unknown_receipt' and len(texts) == 1:
                return {'data': {}}
            return {'data': {'message_id': 'synthetic-business-question-message' if len(texts) == 1 else 'synthetic-business-result-message'}}

        async def profile(*_args, **_kwargs):
            return {'open_id': subject, 'name': 'Business owner'}

        async def download(*_args, **_kwargs):
            if state == 'image_unavailable':
                raise ConnectionError('Synthetic image transport unavailable')
            return image_bytes

        monkeypatch.setattr(FeishuService, 'send_message', send)
        monkeypatch.setattr(feishu, '_resolve_feishu_sender_profile', profile)
        monkeypatch.setattr(FeishuService, 'download_message_resource', download)
        outbox = ChannelDeliveryOutboxService(session_factory=factory)
        await outbox.drain_once(worker_id='quoted-business-question')
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            run = await db.get(RuntimeTask, run_id)
            child_id, parent_id = run.child_session_id, run.parent_session_id
            child = await db.get(ChatSession, uuid.UUID(child_id))
            permission_mode = (child.transcript_metadata_json or {}).get('permission_mode')
            task_id = run.metadata_json['business_task_id']
            notice = await db.scalar(select(ChannelDeliveryOutbox).where(
                ChannelDeliveryOutbox.runtime_task_id == run_id, ChannelDeliveryOutbox.delivery_kind == 'interactive_prompt'))
            assert notice.status == 'delivered'
            receipt = notice.delivery_receipts_json['text']
            assert receipt['detail'].get('provider_message_id') == (
                None if state == 'unknown_receipt' else 'synthetic-business-question-message'), receipt
            question_id = notice.prompt_key
            initial_runs = set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed['tenant_id'])))
            body = {'header': {'event_type': 'im.message.receive_v1', 'event_id': uuid.uuid4().hex},
                'event': {'sender': {'sender_id': {'open_id': subject}}, 'message': {
                    'message_id': uuid.uuid4().hex, 'message_type': 'text', 'chat_type': 'p2p', 'chat_id': 'oc-business-dm',
                    'parent_id': 'synthetic-business-question-message',
                    'content': json.dumps({'text': answer_text})}}}
            if state == 'ordinary':
                body['event']['message'].pop('parent_id')
            if state in {'image', 'vision_image', 'image_unavailable'}:
                body['event']['message'].update(message_type='post', content=json.dumps({'content': [[
                    {'tag': 'text', 'text': answer_text}, {'tag': 'img', 'image_key': 'synthetic-quoted-image'}]]}))
            if state == 'wrong_sender':
                from app.services.external_principal_service import bind_authenticated_self_channel_principal

                other_id = uuid.uuid4()
                db.add(User(id=other_id, tenant_id=seed['tenant_id'], username=f'other-{other_id.hex}',
                    email=f'{other_id}@fixture.test', display_name='Other company user', password_hash='synthetic-unused',
                    role='org_admin', is_active=True))
                await db.flush()
                config = await db.get(ChannelConfig, notice.channel_config_id)
                await bind_authenticated_self_channel_principal(db, tenant_id=seed['tenant_id'], config=config,
                    provider_subject_id=subject, user_id=other_id, actor_user_id=other_id)
            admitted = await enqueue_channel_ingress_event(db, ChannelIngressSubmission(
                tenant_id=seed['tenant_id'], agent_id=seed['agent_id'], provider='feishu',
                installation_ref=str(notice.channel_config_id), provider_event_id=body['header']['event_id'],
                handler_key='feishu.event', payload={'body': body},
                metadata={'feishu_message_scope': feishu_message_scope(await db.get(ChannelConfig, notice.channel_config_id))}))
            await db.commit()
        inbox = ChannelIngressInboxService(session_factory=factory)
        if state == 'withdrawn':
            original = context['original']['original_attachment']
            response = await context['client'].post(f"/chat/attachments/{original['id']}/withdraw", headers=context['headers'],
                json={'expected_content_sha256': original['content_sha256']})
            assert response.status_code == 200, response.text
        elif state == 'cancelled':
            response = await context['client'].post(f"/agents/{seed['agent_id']}/tasks/{task_id}/cancel",
                headers=context['headers'], json={'reason': 'Stop before the quoted answer.'})
            assert response.status_code == 200, response.text
        elif state == 'application_changed':
            async with factory() as db:
                await pin_rls_tenant_context(db, seed['tenant_id'])
                config = await db.get(ChannelConfig, notice.channel_config_id)
                config.app_id = 'synthetic-replaced-provider-app'
                await db.commit()
        if state == 'image_unavailable':
            counts = await inbox.drain_once(worker_id='quoted-business-answer')
            assert counts['retried'] == 1 and counts['processed'] == 0, counts
            async with factory() as db:
                await pin_rls_tenant_context(db, seed['tenant_id'])
                event = await db.get(ChannelIngressEvent, admitted.event_id)
                assert event.status == 'failed' and await db.get(SessionTurnInput, admitted.event_id) is None
                assert set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed['tenant_id']))) == initial_runs
            assert len(model.requests) == 2
            from datetime import UTC, datetime

            async def restored_download(*_args, **_kwargs):
                return image_bytes

            monkeypatch.setattr(FeishuService, 'download_message_resource', restored_download)
            async with factory() as db:
                await pin_rls_tenant_context(db, seed['tenant_id'])
                event = await db.get(ChannelIngressEvent, admitted.event_id)
                event.available_at = datetime.now(UTC)
                await db.commit()
        items = await inbox.claim_batch(worker_id='quoted-business-answer', limit=10)
        assert [row.id for row in items] == [admitted.event_id]
        if state in {'withdrawn', 'cancelled', 'wrong_sender', 'unknown_receipt', 'ordinary', 'application_changed'}:
            dispatched = await dispatch_channel_ingress_event(items[0])
            async with factory() as db:
                await pin_rls_tenant_context(db, seed['tenant_id'])
                current = set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed['tenant_id'])))
                answer = await db.get(SessionTurnInput, admitted.event_id)
                if state == 'ordinary':
                    assert initial_runs < current and len(current - initial_runs) == 1
                    assert answer is not None and answer.intent != 'answer_request' and answer.target_run_id != run_id
                else:
                    assert current == initial_runs
                    assert answer is None or answer.status == 'rejected'
                    assert dispatched['answer_status'] in {'rejected', 'needs_reconciliation'}, dispatched
                    assert await db.scalar(select(ChatMessage.id).where(ChatMessage.source_ingress_event_id == admitted.event_id)) is None
            assert len(model.requests) == 2
            await outbox.drain_once(worker_id='quoted-business-question')
            assert await inbox._mark_processed(item=items[0], worker_id='quoted-business-answer', receipt=dispatched)
            return
        original_payload = copy.deepcopy(items[0].payload)
        for _attempt in range(2):
            dispatched = await dispatch_channel_ingress_event(items[0])
            assert items[0].payload == original_payload
            assert dispatched['runtime_task_id'] == str(run_id) and dispatched['session_id'] == parent_id, dispatched
            async with factory() as db:
                await pin_rls_tenant_context(db, seed['tenant_id'])
                answer = await db.get(SessionTurnInput, admitted.event_id)
                assert answer.intent == 'answer_request' and answer.target_run_id == run_id
                assert answer.session_id == uuid.UUID(child_id) and str(answer.request_item_id) == question_id
                assert answer.status == 'queued'
                child = await db.get(ChatSession, uuid.UUID(child_id))
                assert (child.transcript_metadata_json or {}).get('permission_mode') == permission_mode
                if state in {'image', 'vision_image', 'image_unavailable'}:
                    from app.models.workspace_resource import WorkspaceResourceManifest

                    manifests = list(await db.scalars(select(WorkspaceResourceManifest).where(
                        WorkspaceResourceManifest.agent_id == seed['agent_id'], WorkspaceResourceManifest.source == 'feishu_upload')))
                    assert len(manifests) == 1 and manifests[0].content_hash == hashlib.sha256(image_bytes).hexdigest()
                    assert len(answer.content_parts_json) == 2
                assert await db.scalar(select(ChatMessage.id).where(ChatMessage.source_ingress_event_id == admitted.event_id)) is None
                assert set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed['tenant_id']))) == initial_runs
            assert len(model.requests) == 2
        if state == 'vision_image':
            from app.models.agent import Agent
            from app.models.llm import LLMModel

            async with factory() as db:
                await pin_rls_tenant_context(db, seed['tenant_id'])
                agent = await db.get(Agent, seed['agent_id'])
                selected = await db.get(LLMModel, agent.primary_model_id)
                selected.supports_vision = True
                await db.commit()
        from app.services import runtime_task_worker
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            claims = await RuntimeTaskClaimService(db=db, worker_id='quoted-business-answer',
                task_types=('business_task',)).claim_available(batch_size=10)
            assert [row.id for row in claims] == [run_id]
            version = claims[0].claim_version
        fence = set_runtime_task_fence(task_id=run_id, worker_id='quoted-business-answer', claim_version=version)
        try:
            await runtime_task_worker._execute_claimed_business_task(run_id)
        finally:
            reset_runtime_task_fence(fence)
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            run = await db.get(RuntimeTask, run_id)
            assert run.status == 'completed' and run.child_session_id == child_id
        assert len(model.requests) == 3
        assert answer_text in json.dumps(model.requests[-1]['messages'])
        wire = json.dumps(model.requests[-1]['messages'])
        if state == 'vision_image':
            assert '"image_url"' in wire and 'data:image/jpeg;base64,' in wire
            assert 'the current model does not support vision' not in wire
        elif state in {'image', 'image_unavailable'}:
            assert 'the current model does not support vision' in wire and '"image_url"' not in wire
        replay = await dispatch_channel_ingress_event(items[0])
        assert replay['runtime_task_id'] == str(run_id) and len(model.requests) == 3, replay
        assert await inbox._mark_processed(item=items[0], worker_id='quoted-business-answer', receipt=replay)
        await outbox.drain_once(worker_id='quoted-business-question')
        assert len(texts) == 2 and all(recipient == 'ou-business-owner' for recipient, _text in texts)

    await _business_source(original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        withdrawn=False, unknown_effect=False, question_handler=inspect)
