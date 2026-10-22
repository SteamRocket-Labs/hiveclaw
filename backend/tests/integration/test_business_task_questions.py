"""Business questions keep the original work and same native execution run."""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.task import Task
from tests.integration.test_business_task_text_sources import (
    original_api, provider, delivered, s3_server,  # noqa: F401
    test_business_task_text_rechecks_its_actual_original_source as _business_source,
)

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('provider', ['filesystem'], indirect=True)
@pytest.mark.parametrize('action', ['answer', 'cancel_task', 'withdraw_source', 'cancel_answer', 'revise_answer', 'cancel_race'])
async def test_business_question_answer_resumes_its_original_run(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, action,  # noqa: F811
):
    async def inspect_question(**context):
        seed, factory, client, headers = context['seed'], context['db_factory'], context['client'], context['headers']
        run_id, model = context['run_id'], context['model_client']
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            run = await db.get(RuntimeTask, run_id)
            task = await db.get(Task, uuid.UUID(run.metadata_json['business_task_id']))
            question = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == run_id,
                ChatTranscriptEvent.item_kind == 'user_question', ChatTranscriptEvent.lifecycle == 'waiting'))
            observed = {'run_status': run.status, 'task_status': task.status, 'metadata': run.metadata_json,
                'question_id': str(question.item_id) if question else None}
            (tmp_path / 'business-question-state.json').write_text(json.dumps(observed, indent=2, default=str))
            assert run.status == 'suspended' and task.status == 'doing', observed
            assert task.last_execution_status == 'awaiting_user' and task.completed_at is None
            assert question is not None and len(model.requests) == 2
            question_id, child_id, task_id = str(question.item_id), run.child_session_id, str(task.id)
        detail = await client.get(f"/agents/{seed['agent_id']}/tasks/{task_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert [row['question_item_id'] for row in detail.json()['questions']] == [question_id], detail.text
        assert detail.json()['questions'][0]['can_answer'] is True
        assert 'PRIVATE_BUSINESS_INPUT' in detail.text
        answer_id = str(uuid.uuid4())
        answer = {'input_id': answer_id, 'question_item_id': question_id,
            'content_parts': [{'type': 'text', 'text': 'Report for the original work.'}]}
        path = f"/agents/{seed['agent_id']}/tasks/{task_id}/answers"
        wrong_question = await client.post(path, headers=headers, json={**answer, 'question_item_id': str(uuid.uuid4())})
        assert wrong_question.status_code == 404, wrong_question.text
        if action == 'cancel_race':
            from app.api import tasks as task_api
            from app.services import business_task_waits

            answer_locked, cancel_loaded, release_answer = asyncio.Event(), asyncio.Event(), asyncio.Event()
            project, load = business_task_waits._business_projection, task_api._load_authorized_task

            async def hold_answer(db, run):
                answer_locked.set()
                await asyncio.wait_for(release_answer.wait(), 10)
                return await project(db, run)

            async def observe_cancel(*args, **kwargs):
                result = await load(*args, **kwargs)
                if kwargs.get('action') == 'execute':
                    cancel_loaded.set()
                return result

            monkeypatch.setattr(business_task_waits, '_business_projection', hold_answer)
            monkeypatch.setattr(task_api, '_load_authorized_task', observe_cancel)
            pending_answer = asyncio.create_task(client.post(path, headers=headers, json=answer))
            await asyncio.wait_for(answer_locked.wait(), 10)
            pending_cancel = asyncio.create_task(client.post(f"/agents/{seed['agent_id']}/tasks/{task_id}/cancel",
                headers=headers, json={'reason': 'Stop while the answer is admitted.'}))
            try:
                await asyncio.wait_for(cancel_loaded.wait(), 10)
            finally:
                release_answer.set()
            results = await asyncio.wait_for(asyncio.gather(pending_answer, pending_cancel, return_exceptions=True), 15)
            assert all(not isinstance(row, BaseException) for row in results), results
            assert results[0].status_code == results[1].status_code == 200, [row.text for row in results]
            assert results[1].json()['task']['status'] == 'needs_reconciliation'
            assert len(model.requests) == 2
            from tests.integration.test_business_task_permission_resume import _consume_notice

            await _consume_notice(context)
            return
        if action == 'answer':
            from app.core.security import create_access_token
            from app.models.user import User

            inspector_id = uuid.uuid4()
            async with owner_sessionmaker() as db:
                db.add(User(id=inspector_id, tenant_id=seed['tenant_id'], username=f'inspector-{inspector_id.hex}',
                    email=f'{inspector_id}@fixture.test', display_name='Company inspector', password_hash='synthetic-unused',
                    role='org_admin', is_active=True))
                await db.commit()
            inspector = {'Authorization': 'Bearer ' + create_access_token(str(inspector_id), 'org_admin', str(seed['tenant_id']))}
            inspection = await client.get(f"/agents/{seed['agent_id']}/tasks/{task_id}", headers=inspector,
                params={'operator_view': 'true', 'operator_reason': 'Inspect waiting task'})
            assert inspection.status_code == 200 and inspection.json()['questions'] == [], inspection.text
            assert 'PRIVATE_BUSINESS_INPUT' not in inspection.text
            unauthorized = await client.post(path, headers=inspector, json=answer)
            assert unauthorized.status_code == 403 and len(model.requests) == 2, unauthorized.text
        if action in {'cancel_task', 'withdraw_source'}:
            if action == 'withdraw_source':
                original = context['original']['original_attachment']
                revoked = await client.post(f"/chat/attachments/{original['id']}/withdraw", headers=headers,
                    json={'expected_content_sha256': original['content_sha256']})
                assert revoked.status_code == 200, revoked.text
                hidden = await client.get(f"/agents/{seed['agent_id']}/tasks/{task_id}", headers=headers)
                assert hidden.status_code == 200 and hidden.json()['questions'] == [], hidden.text
                assert 'PRIVATE_BUSINESS_INPUT' not in hidden.text
                denied = await client.post(path, headers=headers, json=answer)
                assert denied.status_code == 403, denied.text
            stopped = await client.post(f"/agents/{seed['agent_id']}/tasks/{task_id}/cancel", headers=headers,
                json={'reason': 'Stop this waiting task.'})
            assert stopped.status_code == 200 and stopped.json()['questions'] == [], stopped.text
            if action == 'cancel_task':
                late = await client.post(path, headers=headers, json=answer)
                assert late.status_code == 200 and late.json()['status'] == 'rejected', late.text
            async with factory() as db:
                await pin_rls_tenant_context(db, seed['tenant_id'])
                from app.services.runtime_task_claim_service import RuntimeTaskClaimService

                assert await RuntimeTaskClaimService(db=db, worker_id='business-question',
                    task_types=('business_task',)).claim_available(batch_size=10) == []
            assert len(model.requests) == 2
            from tests.integration.test_business_task_permission_resume import _consume_notice

            await _consume_notice(context)
            return
        submitted = await client.post(path, headers=headers, json=answer)
        assert submitted.status_code == 200 and submitted.json()['status'] == 'queued', submitted.text
        repeated = await client.post(path, headers=headers, json=answer)
        assert repeated.status_code == 200 and repeated.json()['replayed'] is True, repeated.text
        if action in {'cancel_answer', 'revise_answer'}:
            from app.api import chat_sessions

            client._transport.app.include_router(chat_sessions.router)
            input_path = f"/agents/{seed['agent_id']}/sessions/{child_id}/inputs/{answer_id}"
            if action == 'cancel_answer':
                cancelled = await client.delete(input_path, headers={**headers, 'If-Match': str(submitted.json()['revision'])})
                assert cancelled.status_code == 200 and cancelled.json()['status'] == 'cancelled', cancelled.text
                async with factory() as db:
                    await pin_rls_tenant_context(db, seed['tenant_id'])
                    paused = await db.get(RuntimeTask, run_id)
                    assert paused.status == 'suspended', paused.metadata_json
                reopened = await client.get(f"/agents/{seed['agent_id']}/tasks/{task_id}", headers=headers)
                assert reopened.status_code == 200 and len(reopened.json()['questions']) == 1, reopened.text
                next_question = reopened.json()['questions'][0]['question_item_id']
                assert next_question != question_id
                resubmitted = await client.post(path, headers=headers, json={**answer,
                    'input_id': str(uuid.uuid4()), 'question_item_id': next_question})
                assert resubmitted.status_code == 200 and resubmitted.json()['status'] == 'queued', resubmitted.text
            else:
                revised = await client.patch(input_path, headers={**headers, 'If-Match': str(submitted.json()['revision'])},
                    json={'content_parts': [{'type': 'text', 'text': 'Revised report for the original work.'}]})
                assert revised.status_code == 200 and revised.json()['status'] == 'queued', revised.text
        from app.services import runtime_task_worker
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            claims = await RuntimeTaskClaimService(db=db, worker_id='business-question',
                task_types=('business_task',)).claim_available(batch_size=10)
            assert [row.id for row in claims] == [run_id]
            version = claims[0].claim_version
        fence = set_runtime_task_fence(task_id=run_id, worker_id='business-question', claim_version=version)
        try:
            await runtime_task_worker._execute_claimed_business_task(run_id)
        finally:
            reset_runtime_task_fence(fence)
        async with factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            run = await db.get(RuntimeTask, run_id)
            assert run.status == 'completed' and run.child_session_id == child_id, run.metadata_json
            assert len(model.requests) == 3
            expected_answer = 'Revised report for the original work.' if action == 'revise_answer' else 'Report for the original work.'
            assert expected_answer in json.dumps(model.requests[-1]['messages'])
        from tests.integration.test_business_task_permission_resume import _consume_notice

        await _consume_notice(context)

    await _business_source(original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        withdrawn=False, unknown_effect=False, question_handler=inspect_question)
