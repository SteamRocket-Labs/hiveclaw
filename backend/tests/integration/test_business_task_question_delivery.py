"""Pending native business questions return through the original channel safely."""

import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.runtime_task import RuntimeTask
from tests.integration.test_business_task_text_sources import (
    original_api, provider, delivered, s3_server,  # noqa: F401
    test_business_task_text_rechecks_its_actual_original_source as _business_source,
)

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('provider', ['filesystem'], indirect=True)
@pytest.mark.parametrize('state', ['waiting', 'withdrawn', 'cancelled', 'answered', 'parent_target_changed', 'group_waiting', 'group_withdrawn', 'group_answered', 'group_quoted'])
async def test_pending_business_question_returns_to_its_original_channel(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, state,  # noqa: F811
):
    group_case = state.startswith('group_')
    state = state.removeprefix('group_')
    quoted = state == 'quoted'
    if quoted:
        from app.api import feishu

        async def profile(*_args, **_kwargs):
            return {'open_id': 'ou-business-owner', 'name': 'Business owner'}
        monkeypatch.setattr(feishu, '_resolve_feishu_sender_profile', profile)
        state = 'waiting'
    if state == 'parent_target_changed':
        from app.models.chat_session import ChatSession
        from app.services import business_task_question_delivery

        enqueue = business_task_question_delivery.enqueue_business_question_notice

        async def change_parent_target(db, *, run, question_id):
            session = await db.get(ChatSession, uuid.UUID(run.parent_session_id))
            session.delivery_target_json = {**session.delivery_target_json, 'receive_id': 'ou-different-work'}
            return await enqueue(db, run=run, question_id=question_id)

        monkeypatch.setattr(business_task_question_delivery, 'enqueue_business_question_notice', change_parent_target)

    async def inspect(**context):
        seed, factory, client, headers = context['seed'], context['db_factory'], context['client'], context['headers']
        run_id, model = context['run_id'], context['model_client']
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            run = await db.get(RuntimeTask, run_id)
            assert run.status == 'suspended' and len(model.requests) == 2
            task_id, child_id = run.metadata_json['business_task_id'], run.child_session_id
            notices = list(await db.scalars(select(ChannelDeliveryOutbox).where(
                ChannelDeliveryOutbox.runtime_task_id == run_id, ChannelDeliveryOutbox.delivery_kind == 'interactive_prompt')))
            assert len(notices) == 1
            notice_id = notices[0].id
            assert notices[0].session_id == uuid.UUID(run.parent_session_id)
            if group_case:
                assert notices[0].delivery_target_json['receive_id'] == 'oc_business_group'
            else:
                assert notices[0].delivery_target_json['receive_id'] == 'ou-business-owner'
            assert notices[0].metadata_json['projection_kind'] == 'question_notice'
            question_id = notices[0].prompt_key
            assert question_id and 'PRIVATE_BUSINESS_INPUT' in notices[0].text_content
            assert f'task_id={task_id}#tasks' in notices[0].text_content
        texts = []
        from app.services.feishu_service import FeishuService
        from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService

        async def send(self, _app, _secret, recipient, _kind, text, **_kwargs):
            texts.append((recipient, text))
            return {'data': {'message_id': f'synthetic-question-{len(texts)}'}}

        monkeypatch.setattr(FeishuService, 'send_message', send)
        api = f"/agents/{seed['agent_id']}/tasks/{task_id}"

        async def answer():
            if quoted:
                from tests.integration.original_work_permission_helpers import claim_permission_reply
                from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event

                inbox, item = await claim_permission_reply(factory=factory, tenant_id=seed['tenant_id'],
                    notice_id=notice_id, text='Report for this original work.')
                receipt = await dispatch_channel_ingress_event(item)
                assert receipt.get('answer_status') == 'queued', receipt
                assert await inbox._mark_processed(item=item, worker_id='permission-quote', receipt=receipt)
                assert await inbox.claim_batch(worker_id='answered-group-question', limit=10) == []
                return
            response = await client.post(api + '/answers', headers=headers, json={
                'input_id': str(uuid.uuid4()), 'question_item_id': question_id,
                'content_parts': [{'type': 'text', 'text': 'Report for this original work.'}]})
            assert response.status_code == 200 and response.json()['status'] == 'queued', response.text

        if state == 'withdrawn':
            original = context['original']['original_attachment']
            response = await client.post(f"/chat/attachments/{original['id']}/withdraw", headers=headers,
                json={'expected_content_sha256': original['content_sha256']})
            assert response.status_code == 200, response.text
        elif state == 'cancelled':
            response = await client.post(api + '/cancel', headers=headers, json={'reason': 'Stop before question delivery.'})
            assert response.status_code == 200, response.text
        elif state == 'answered':
            await answer()
        service = ChannelDeliveryOutboxService(session_factory=factory)
        await service.drain_once(worker_id='business-question-delivery')
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            notice = await db.get(ChannelDeliveryOutbox, notice_id)
            assert notice.status == ('delivered' if state in {'waiting', 'parent_target_changed'} else 'dead_letter'), notice.last_error
        if group_case:
            assert all(recipient == 'oc_business_group' for recipient, _text in texts)
        else:
            assert all(recipient == 'ou-business-owner' for recipient, _text in texts)
        assert sum('PRIVATE_BUSINESS_INPUT' in text for _recipient, text in texts) == int(state in {'waiting', 'parent_target_changed'})
        assert (await service.drain_once(worker_id='business-question-delivery'))['claimed'] == 0
        if state not in {'waiting', 'answered', 'parent_target_changed'}:
            assert len(model.requests) == 2
            return
        if state in {'waiting', 'parent_target_changed'}:
            await answer()
        from app.services import runtime_task_worker
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            claims = await RuntimeTaskClaimService(db=db, worker_id='business-question-delivery',
                task_types=('business_task',)).claim_available(batch_size=10)
            assert [row.id for row in claims] == [run_id]
            version = claims[0].claim_version
        fence = set_runtime_task_fence(task_id=run_id, worker_id='business-question-delivery', claim_version=version)
        try:
            await runtime_task_worker._execute_claimed_business_task(run_id)
        finally:
            reset_runtime_task_fence(fence)
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            run = await db.get(RuntimeTask, run_id)
            assert run.status == 'completed' and run.child_session_id == child_id and len(model.requests) == 3
        await service.drain_once(worker_id='business-question-delivery')
        assert len(texts) == (2 if state in {'waiting', 'parent_target_changed'} else 1)

    await _business_source(original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        withdrawn=False, unknown_effect=False, question_handler=inspect,
        group_delivery_handler=inspect if group_case else None)
