"""Observe original-work consumption at a real native model commit boundary."""

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import pin_rls_tenant_context

PROGRESS = 'NATIVE_FIRST_TASK_PROGRESS: I am preparing the authorized report.'
PRIVATE_REASONING = 'PRIVATE_REASONING_MUST_NOT_BE_PROGRESS'


def observe_first_task_progress(*, seed, task_id, draft_id, factory, owner_factory, monkeypatch, observations):
    from app.api import hr_creation
    from app.core.security import get_current_user
    from app.database import get_db
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.session_v2 import SessionModelResult
    from app.models.user import User
    from app.services.delegation_session_runtime import DelegationSessionRuntime

    commit = DelegationSessionRuntime.commit
    async def inspect(runtime, **payload):
        result = await commit(runtime, **payload)
        if runtime.scope['run_id'] != task_id or (payload.get('response') or {}).get('content') != PROGRESS:
            return result
        async with owner_factory() as db:
            user = await db.get(User, seed['user_id'])
        async def database():
            async with factory() as db:
                await pin_rls_tenant_context(db, seed['tenant_id'])
                yield db
        app = FastAPI()
        app.include_router(hr_creation.router, prefix='/api')
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = lambda: user
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://progress.local') as api:
            view = await api.get(f"/api/agents/{seed['agent_id']}/hr-creation-drafts/{draft_id}")
        observations['progress_app'] = app
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == task_id,
                SessionModelResult.provider_request_id == payload['provider_request_id']))
            notices = list(await db.scalars(select(ChannelDeliveryOutbox).where(
                ChannelDeliveryOutbox.runtime_task_id == task_id,
                ChannelDeliveryOutbox.metadata_json['source'].astext == 'original_work_progress')))
            observations['progress'] = {'http_status': view.status_code, 'draft': view.json(),
                'model_state': model.state, 'model_result_id': str(model.id),
                'notice_ids': [str(row.id) for row in notices]}
        if notices:
            try:
                await inspect_progress_delivery(seed=seed, factory=factory, owner_factory=owner_factory,
                    app=app, task_id=task_id, draft_id=draft_id, notice_id=notices[0].id,
                    provider_request_id=payload['provider_request_id'], observations=observations)
            except Exception as exc:
                observations['progress_checks_error'] = {'type': type(exc).__name__, 'message': str(exc)}
        return result
    monkeypatch.setattr(DelegationSessionRuntime, 'commit', inspect)


async def inspect_progress_delivery(*, seed, factory, owner_factory, app, task_id, draft_id, notice_id,
                                    provider_request_id, observations):
    from app.core.security import get_current_user
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService, _claimed
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.feishu_service import FeishuService
    from app.services.original_work_progress import enqueue_original_work_progress

    service = ChannelDeliveryOutboxService(session_factory=factory)
    async with factory() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        notice = await db.get(ChannelDeliveryOutbox, notice_id)
        saved = _claimed(notice)
        run = await db.get(RuntimeTask, task_id)
        child_id = run.child_agent_id
        await service._authorize_native_result_text(db, saved)
        for changed in (
            replace(saved, text=saved.text + '\nUNATTESTED'),
            replace(saved, delivery_target={**saved.delivery_target, 'receive_id': 'another-recipient'}),
            replace(saved, metadata={k: v for k, v in saved.metadata.items() if k != 'source'}),
            replace(saved, metadata={**saved.metadata, 'model_result_id': str(uuid.uuid4())}),
        ):
            with pytest.raises(ContentInputReceiptError):
                await service._authorize_native_result_text(db, changed)
        for _ in range(2):
            await enqueue_original_work_progress(db, run_id=task_id, provider_request_id=provider_request_id)
        await db.commit()
        assert list(await db.scalars(select(ChannelDeliveryOutbox.id).where(
            ChannelDeliveryOutbox.runtime_task_id == task_id,
            ChannelDeliveryOutbox.metadata_json['source'].astext == 'original_work_progress'))) == [notice_id]

    async def get_work():
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://progress.local') as api:
            return await api.get(f"/api/agents/{seed['agent_id']}/hr-creation-drafts/{draft_id}")

    source = seed['data_root'] / str(child_id) / 'soul.md'
    original = source.read_bytes()
    try:
        source.write_bytes(original + b'\nChanged before progress consumption.\n')
        denied = await get_work()
        assert denied.status_code == 200 and denied.json()['first_task_work']['progress'] is None
        assert denied.json()['first_task_work']['status'] == 'unavailable', denied.json()
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            with pytest.raises(ContentInputReceiptError) as denied_send:
                await service._authorize_native_result_text(db, saved)
            assert denied_send.value.status_code == 403
    finally:
        source.write_bytes(original)
    assert (await get_work()).json()['first_task_work']['progress']['text'] == PROGRESS

    current = app.dependency_overrides[get_current_user]
    async with owner_factory() as db:
        other = User(id=uuid.uuid4(), username=uuid.uuid4().hex, email=f'{uuid.uuid4().hex}@synthetic.invalid',
            password_hash='synthetic', display_name='Another company administrator', tenant_id=seed['tenant_id'],
            role='org_admin', is_active=True)
        db.add(other)
        await db.commit()
    app.dependency_overrides[get_current_user] = lambda: other
    try:
        denied = await get_work()
        assert denied.status_code in {200, 403, 404}
        assert PROGRESS not in denied.text and PRIVATE_REASONING not in denied.text
    finally:
        app.dependency_overrides[get_current_user] = current

    sent = []
    async def transport(_self, _app_id, _secret, recipient, message_type, content, **kwargs):
        sent.append((recipient, message_type, json.loads(content)['text'], kwargs['receive_id_type']))
        return {'data': {'message_id': 'synthetic-first-task-progress'}}
    async with factory() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        row = await db.get(ChannelDeliveryOutbox, notice_id)
        # Leave the separate employee-ready notice available to its own worker.
        row.status, row.locked_by, row.locked_at = 'processing', 'first-task-progress', datetime.now(UTC)
        row.attempt_count += 1
        await db.commit()
        claimed = _claimed(row)
    with patch.object(FeishuService, 'send_message', transport):
        delivered = await service._deliver(claimed, worker_id='first-task-progress')
        assert await service._mark_delivered(item_id=claimed.id, worker_id='first-task-progress', receipt=delivered)
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            row = await db.get(ChannelDeliveryOutbox, notice_id)
            assert row.status == 'delivered'
            assert row.delivery_receipts_json['text']['detail']['provider_message_id'] == 'synthetic-first-task-progress'
            await service._deliver(_claimed(row), worker_id='first-task-progress')
    assert sent == [(saved.delivery_target['receive_id'], 'text', saved.text, 'open_id')]
    assert PROGRESS in saved.text and PRIVATE_REASONING not in saved.text
    assert f'/agents/{seed["agent_id"]}?session_id={saved.session_id}#chat' in saved.text
    assert '此提示不代表任务完成' in saved.text
    observations['progress_claimed'] = saved
    observations['progress_checks_passed'] = True


async def assert_completed_progress(*, seed, factory, draft_id, observations):
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.content_input_receipts import ContentInputReceiptError

    app = observations['progress_app']
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://progress.local') as api:
        response = await api.get(f"/api/agents/{seed['agent_id']}/hr-creation-drafts/{draft_id}")
    work = response.json()['first_task_work']
    assert response.status_code == 200 and work['status'] == 'completed' and work['progress'] is None, work
    async with factory() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        with pytest.raises(ContentInputReceiptError) as denied:
            await ChannelDeliveryOutboxService(session_factory=factory)._authorize_native_result_text(
                db, observations['progress_claimed'])
        assert denied.value.code == 'progress_no_longer_current'


def assert_first_task_progress(observations):
    result = observations.get('progress')
    assert result is not None, observations
    assert result['http_status'] == 200 and result['model_state'] == 'round_committed', result
    work = result['draft']['first_task_work']
    assert work['status'] == 'running' and work.get('progress'), result
    assert work['progress']['text'] == PROGRESS and work['progress']['model_result_id'] == result['model_result_id'], result
    assert len(result['notice_ids']) == 1, result
    assert PRIVATE_REASONING not in json.dumps(result), result
    assert 'progress_checks_error' not in observations, observations.get('progress_checks_error')
    assert observations.get('progress_checks_passed') is True
