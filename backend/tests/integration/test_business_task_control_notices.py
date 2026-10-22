"""Task cancellation and expired workers notify their original private work."""

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from tests.integration.test_m4s_original_attachments_postgres import original_api, delivered  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('control', ['queued_cancel', 'running_cancel', 'expired_lease'])
async def test_business_task_controls_notify_once_without_private_content(
    original_api, app_user_sessionmaker, owner_sessionmaker, monkeypatch, tmp_path, control,  # noqa: F811
):
    import app.database as database
    from app.api import tasks
    from app.core.security import create_access_token
    from app.models.channel_config import ChannelConfig
    from app.models.task import Task
    from app.services.business_task_reconciliation import reconcile_stale_business_tasks_once
    from app.services.business_task_runtime import stage_business_task_runtime
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.external_principal_service import bind_authenticated_self_channel_principal
    from app.services.feishu_service import FeishuService
    from app.services import business_task_runtime

    _, client = original_api
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    enqueue = business_task_runtime._enqueue_business_task_channel_delivery

    async def scoped_notice(**kwargs):
        assert await kwargs['db'].scalar(text("SELECT current_setting('app.current_tenant_id', true)")) == str(seed['tenant_id'])
        return await enqueue(**kwargs)

    monkeypatch.setattr(business_task_runtime, '_enqueue_business_task_channel_delivery', scoped_notice)
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, '__name__', '').startswith('app.'):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)

    async def no_remote_wakeup(**_kwargs):
        pass

    monkeypatch.setattr('app.services.runtime_control_bus.publish_business_task_cancel', no_remote_wakeup)
    client._transport.app.include_router(tasks.router)
    headers = {'Authorization': 'Bearer ' + create_access_token(str(seed['user_id']), 'member', str(seed['tenant_id']))}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        config = ChannelConfig(id=uuid.uuid4(), tenant_id=seed['tenant_id'], agent_id=seed['agent_id'],
            channel_type='feishu', app_id='synthetic-control', app_secret='synthetic-control-only', is_configured=True)
        db.add(config)
        await db.flush()
        bound = await bind_authenticated_self_channel_principal(db, tenant_id=seed['tenant_id'], config=config,
            provider_subject_id='ou-control-owner', user_id=seed['user_id'], actor_user_id=seed['user_id'])
        task = Task(id=uuid.uuid4(), tenant_id=seed['tenant_id'], agent_id=seed['agent_id'],
            created_by=seed['user_id'], responsible_user_id=seed['user_id'], root_session_id=seed['session_id'],
            title='PRIVATE_TASK_TITLE', description='PRIVATE_TASK_DESCRIPTION', request_id=str(uuid.uuid4()),
            request_hash='a' * 64, authority_state='owned')
        db.add(task)
        await db.flush()
        run = await stage_business_task_runtime(db=db, task=task, requester_user_id=seed['user_id'],
            agent_name='Control employee', request_id=task.request_id, request_hash=task.request_hash,
            root_session_id=seed['session_id'], delivery_target={'channel': 'feishu', 'receive_id': 'ou-control-owner',
                'receive_id_type': 'open_id', 'chat_type': 'p2p', 'session_id': str(seed['session_id']),
                'external_principal_id': str(bound.principal.id)})
        if control != 'queued_cancel':
            task.status, task.last_execution_status = 'doing', 'running'
            run.status, run.claimed_by, run.claim_version = 'running', 'lost-business-worker', 1
            run.claim_expires_at = datetime.now(UTC) + timedelta(minutes=-1 if control == 'expired_lease' else 10)
            run.metadata_json = {**run.metadata_json, 'phase': 'invoking'}
        task_id, run_id = task.id, run.id
        await db.commit()
    if control == 'expired_lease':
        first = await reconcile_stale_business_tasks_once()
        assert first == {'checked': 1, 'quarantined': 1}
        assert await reconcile_stale_business_tasks_once() == {'checked': 0, 'quarantined': 0}
    else:
        for _ in range(2):
            response = await client.post(f"/agents/{seed['agent_id']}/tasks/{task_id}/cancel", headers=headers,
                json={'reason': 'PRIVATE_CANCEL_REASON'})
            assert response.status_code == 200, response.text
            assert response.json()['task']['status'] == ('cancelled' if control == 'queued_cancel' else 'needs_reconciliation')
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        rows = list((await db.scalars(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == run_id))).all())
        assert len(rows) == 1
        row = rows[0]
        assert row.session_id == seed['session_id'] and row.user_id == seed['user_id']
        assert row.terminal_status == ('killed' if control == 'queued_cancel' else 'needs_reconciliation')
        assert not row.artifact_ids_json
        outbox_id = row.id
    calls = []

    async def transport(self, _app_id, _secret, recipient, *args, **kwargs):
        calls.append({'recipient': recipient, 'args': args, 'kwargs': kwargs})
        return {'data': {'message_id': 'synthetic-control-ack'}}

    monkeypatch.setattr(FeishuService, 'send_message', transport)
    service = ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker)
    first = await service.drain_once(worker_id='business-control')
    assert first['delivered'] == 1 and len(calls) == 1, first
    assert calls[0]['recipient'] == 'ou-control-owner'
    assert not any(value in json.dumps(calls) for value in ('PRIVATE_TASK_TITLE', 'PRIVATE_TASK_DESCRIPTION', 'PRIVATE_CANCEL_REASON'))
    assert ('cancelled' if control == 'queued_cancel' else 'reconciliation') in json.dumps(calls)
    assert (await service.drain_once(worker_id='business-control'))['claimed'] == 0
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        row = await db.get(ChannelDeliveryOutbox, outbox_id)
        assert row.status == 'delivered'
        (tmp_path / 'business-control-notice.json').write_text(json.dumps({
            'control': control, 'run_id': str(run_id), 'delivery_id': str(row.id), 'calls': calls,
            'receipts': row.delivery_receipts_json}, indent=2))
