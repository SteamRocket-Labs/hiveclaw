"""Native task permission waits resume the same run without repeating effects."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation
from app.models.task import Task
from tests.integration.test_business_task_text_sources import (
    original_api, provider, delivered, s3_server,  # noqa: F401
    test_business_task_text_rechecks_its_actual_original_source as _business_source,
)

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('provider', ['filesystem'], indirect=True)
@pytest.mark.parametrize('decision', ['allow_once', 'deny', 'cancel', 'unknown', 'expired'])
async def test_business_permission_wait_resumes_the_original_run(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, decision, migrated_pg_url,  # noqa: F811
):
    async def inspect_wait(**context):
        client, db_factory, seed = context['client'], context['db_factory'], context['seed']
        run_id, model = context['run_id'], context['model_client']
        async with db_factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            run = await db.get(RuntimeTask, run_id)
            task = await db.get(Task, uuid.UUID(run.metadata_json['business_task_id']))
            tool = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == run_id,
                SessionToolInvocation.tool_name == 'fs_write'))
            observed = {'run_status': run.status, 'metadata': run.metadata_json, 'task_status': task.status,
                'last_execution_status': task.last_execution_status, 'completed_at': task.completed_at,
                'tool': {'id': str(tool.id), 'permission_state': tool.permission_state,
                    'effect_state': tool.effect_state, 'result_event_id': tool.result_event_id}}
            (tmp_path / 'business-permission-wait.json').write_text(json.dumps(observed, default=str, indent=2))
            assert run.status == 'suspended', observed
            assert task.status == 'doing' and task.last_execution_status == 'awaiting_permission'
            assert task.completed_at is None and task.last_result is None and task.last_error is None
            assert tool.permission_state == 'waiting' and tool.effect_state == 'prepared_not_started'
            assert tool.result_event_id is None and len(model.requests) == 2
            child_id, permission_id, invocation_id = run.child_session_id, tool.permission_item_id, tool.id
            business_id = str(task.id)
        assert not (seed['agent_root'] / 'workspace/business-report.md').exists()
        detail = await client.get(f"/agents/{seed['agent_id']}/tasks/{business_id}", headers=context['headers'])
        assert detail.status_code == 200, detail.text
        assert detail.json()['task']['reflection_session_id'] == child_id
        assert detail.json()['task']['actions']['can_cancel'] and not detail.json()['task']['actions']['can_retry']
        from app.api import chat_sessions
        from app.services import agent_tools, runtime_task_worker

        client._transport.app.include_router(chat_sessions.router)
        effects = []
        execute = agent_tools.execute_session_permission_tool

        async def actual_effect(*args, **kwargs):
            effects.append(kwargs['tool_call_id'])
            return await execute(*args, **kwargs)

        async def no_wakeup(**_kwargs):
            pass

        monkeypatch.setattr(agent_tools, 'execute_session_permission_tool', actual_effect)
        monkeypatch.setattr(runtime_task_worker, 'notify_runtime_task_worker', no_wakeup)
        path = f"/agents/{seed['agent_id']}/sessions/{child_id}/permissions/{permission_id}/resolve"
        if decision == 'cancel':
            cancelled = await client.post(f"/agents/{seed['agent_id']}/tasks/{business_id}/cancel",
                headers=context['headers'], json={'reason': 'Stop this suspended task.'})
            assert cancelled.status_code == 200, cancelled.text
            late = await client.post(path, headers=context['headers'], json={'action': 'allow_once'})
            assert late.status_code == 409, late.text
            assert effects == [] and len(model.requests) == 2
            assert not (seed['agent_root'] / 'workspace/business-report.md').exists()
            async with db_factory() as db:
                await pin_rls_tenant_context(db, seed['tenant_id'])
                stopped = await db.get(RuntimeTask, run_id)
                assert stopped.status == 'needs_reconciliation'
                claims = await _claims(db)
                assert claims == []
            await _consume_notice(context)
            return
        if decision == 'unknown':
            from app.services import session_tool_runtime

            async def lose_result(*_args, **_kwargs):
                pass

            monkeypatch.setattr(session_tool_runtime, 'complete_tool_invocation', lose_result)
            uncertain = await client.post(path, headers=context['headers'], json={'action': 'allow_once'})
            assert uncertain.status_code == 200 and uncertain.json()['run_status'] == 'needs_reconciliation', uncertain.text
            retry = await client.post(path, headers=context['headers'], json={'action': 'allow_once'})
            assert retry.status_code == 200 and retry.json()['run_status'] == 'needs_reconciliation', retry.text
            detail = await client.get(f"/agents/{seed['agent_id']}/tasks/{business_id}", headers=context['headers'])
            assert detail.status_code == 200 and detail.json()['task']['status'] == 'needs_reconciliation', detail.text
            assert not detail.json()['task']['actions']['can_retry'] and detail.json()['task']['actions']['can_reconcile']
            assert len(effects) == 1 and len(model.requests) == 2
            await _consume_notice(context)
            return
        if decision in {'deny', 'expired'}:
            model.script[-1]['content'] = 'The write was not authorized; no report was created.'
        if decision == 'expired':
            from datetime import UTC, datetime, timedelta
            from app.services.session_permission_runtime import expire_stale_session_permission_requests

            async with db_factory() as db:
                await pin_rls_tenant_context(db, seed['tenant_id'])
                pending = await db.get(SessionToolInvocation, invocation_id)
                pending.permission_expires_at = datetime.now(UTC) - timedelta(minutes=1)
                await db.commit()
                assert await expire_stale_session_permission_requests(db=db) == 1
                await db.refresh(pending)
                assert pending.permission_state == 'expired' and pending.result_event_id is not None
                from app.models.chat_transcript_event import ChatTranscriptEvent
                from app.services.session_model_inputs import _expired_permission_result_bound, _origin_dependencies
                from app.services.session_semantic_history import _event_payload

                _sources, complete = await _origin_dependencies(db, tenant_id=seed['tenant_id'], agent_id=seed['agent_id'],
                    ref=f'session-tool-result:{pending.id}')
                assert complete
                result_event = await db.get(ChatTranscriptEvent, pending.result_event_id)
                content = _event_payload(result_event)['content']
                assert await _expired_permission_result_bound(db, invocation=pending, event=result_event, content=content)
                forged = {**json.loads(content), 'content': 'UNATTESTED_WORKSPACE_BYTES'}
                assert not await _expired_permission_result_bound(db, invocation=pending, event=result_event, content=json.dumps(forged))
        else:
            accepted = await client.post(path, headers=context['headers'], json={'action': decision})
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()['run_id'] == str(run_id) and accepted.json()['run_status'] == 'resumable'
            again = await client.post(path, headers=context['headers'], json={'action': decision})
            assert again.status_code == 200 and len(effects) == (1 if decision == 'allow_once' else 0), again.text
        if decision == 'allow_once':
            assert (seed['agent_root'] / 'workspace/business-report.md').read_text() == context['artifact_body']
        from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

        async with db_factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            claims = await _claims(db)
            assert [row.id for row in claims] == [run_id]
            version = claims[0].claim_version
        fence = set_runtime_task_fence(task_id=run_id, worker_id='business-permission-resume', claim_version=version)
        try:
            await runtime_task_worker._execute_claimed_business_task(run_id)
        finally:
            reset_runtime_task_fence(fence)
        async with db_factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            run = await db.get(RuntimeTask, run_id)
            tool = await db.get(SessionToolInvocation, invocation_id)
            assert run.status == 'completed' and run.child_session_id == child_id, run.metadata_json
            assert tool.permission_state == {'allow_once': 'approved', 'deny': 'denied', 'expired': 'expired'}[decision]
            assert tool.result_event_id is not None
            assert len(effects) == (1 if decision == 'allow_once' else 0) and len(model.requests) == 3
        if decision != 'allow_once':
            assert not (seed['agent_root'] / 'workspace/business-report.md').exists()
            await _consume_notice(context)
            return
        from tests.integration.test_business_task_text_sources import _assert_business_file_return

        await _assert_business_file_return(withdrawn=False, **context)
        await _verify_permission_binding(context, child_id, uuid.UUID(accepted.json()['control_id']), migrated_pg_url)

    await _business_source(original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        withdrawn=False, unknown_effect=False, artifact_body='# Report\nPRIVATE_BUSINESS_INPUT\n',
        permission_handler=inspect_wait)


async def _claims(db):
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    return await RuntimeTaskClaimService(db=db, worker_id='business-permission-resume',
        task_types=('business_task',)).claim_available(batch_size=10)


async def _consume_notice(context):
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.feishu_service import FeishuService

    texts, files = [], []

    async def text_transport(self, _app_id, _secret, recipient, *args, **kwargs):
        texts.append(recipient)
        return {'data': {'message_id': 'synthetic-business-permission-ack'}}

    async def file_transport(self, *args, **kwargs):
        files.append((args, kwargs))

    context['monkeypatch'].setattr(FeishuService, 'send_message', text_transport)
    context['monkeypatch'].setattr(FeishuService, 'upload_and_send_file', file_transport)
    service = ChannelDeliveryOutboxService(session_factory=context['db_factory'])
    counts = await service.drain_once(worker_id='business-permission-notice')
    assert texts == ['ou-business-owner'] and files == [], counts
    assert (await service.drain_once(worker_id='business-permission-notice'))['claimed'] == 0


async def _verify_permission_binding(context, child_id, control_id, url):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionControlInput
    from tests.integration.test_business_task_child_binding import _migrate

    seed, factory, owner = context['seed'], context['db_factory'], context['owner_db_factory']
    async with factory() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        control = await db.get(SessionControlInput, control_id)
        control.kind = 'cancel_run'
        with pytest.raises(DBAPIError, match='session_v2_authority_binding_mismatch'):
            await db.flush()
        await db.rollback()
    async with owner() as db:
        child = await db.get(ChatSession, uuid.UUID(child_id))
        child.user_id = None
        await db.commit()
    try:
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            control = await db.get(SessionControlInput, control_id)
            control.version += 1
            with pytest.raises(DBAPIError, match='session_v2_authority_binding_mismatch'):
                await db.flush()
            await db.rollback()
    finally:
        async with owner() as db:
            (await db.get(ChatSession, uuid.UUID(child_id))).user_id = seed['user_id']
            await db.commit()
    refused = _migrate(url, 'downgrade', 'business_task_child_1022', context['tmp_path'])
    assert refused.returncode != 0 and 'Retain native business permission responses before downgrade' in refused.stderr
    async with owner() as db:
        assert await db.scalar(text('SELECT version_num FROM alembic_version')) == 'business_permissions_1022'
        assert await db.get(SessionControlInput, control_id) is not None
