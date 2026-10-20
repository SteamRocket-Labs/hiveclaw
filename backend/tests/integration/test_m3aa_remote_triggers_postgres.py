"""A real scheduled input retains autonomous authority and the original budget."""

import asyncio
from copy import deepcopy
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
from app.services.runtime_task_claim_service import RuntimeTaskClaimService
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


@pytest.mark.parametrize('fault', [None, 'budget_stopped', 'trigger_revoked', 'origin_tamper', 'model_pin'])
async def test_original_work_routine_uses_remote_without_a_native_model_and_keeps_its_authority(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path, fault
):
    from app.api import chat_sessions, plans, triggers
    from app.models.agent import Agent
    from app.services import runtime_task_worker, trigger_daemon
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.session_input_dispatch import recover_admitted_session_inputs_once

    seed, api, session_id = remote_work
    for router in (chat_sessions.router, plans.router, triggers.router):
        api._transport.app.include_router(router)
    await original.prepare_employee(seed, app_user_sessionmaker, monkeypatch, tmp_path)
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        agent = await db.get(Agent, seed['agent'])
        agent.primary_model_id = agent.fallback_model_id = None
        await db.commit()
    base = f"/agents/{seed['agent']}"
    async with sdk_receiver(tmp_path / 'receiver', True) as receiver:
        path = await original.original.configure(seed, api, receiver)
        assert (await api.post(path + '/runtime', json=selection(seed))).status_code == 200
        saved = await api.post(base + '/triggers', json={'name': 'remote_original_work', 'type': 'interval',
            'config': {'minutes': 30, 'delivery': 'same_session', 'source_session_id': str(session_id),
                **({'model_id': str(uuid4())} if fault == 'model_pin' else {})},
            'reason': 'REMOTE_SCHEDULED_ASSIGNMENT: compare the current evidence in this original work.', 'is_enabled': False})
        assert saved.status_code == 201, saved.text
        recommendation = await api.post(base + '/plan-recommendations', json={
            'original_request': 'Enable this routine without a separate plan.', 'session_id': str(session_id),
            'source': 'agent_aware', 'action_kind': 'enable_autonomous_wake', 'tool_name': 'trigger_rest'})
        assert recommendation.status_code == 201, recommendation.text
        declined = await api.post(base + '/plan-recommendations/' + recommendation.json()['id'] + '/decline')
        assert declined.status_code == 200, declined.text
        enabled = await api.patch(base + '/triggers/' + saved.json()['id'], json={'is_enabled': True,
            'plan_mode_decision': 'declined', 'plan_recommendation_id': declined.json()['id']})
        assert enabled.status_code == 200, enabled.text
        fired = await trigger_daemon.fire_trigger_once_now(seed['agent'], saved.json()['id'])
        if fault == 'model_pin':
            assert fired['fired'] is False and fired['reason'] == 'remote_a2a_model_pin_unavailable', fired
            assert not (receiver.directory / 'consumed-0.json').exists()
            return
        assert fired['fired'], fired
        wrapper_id = UUID(fired['runtime_task_id'])
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            claims = await RuntimeTaskClaimService(db=db, worker_id='remote-trigger', task_types=('trigger',)).claim_available()
            claim = next(task for task in claims if task.id == wrapper_id)
        assert runtime_task_worker._dispatch_claimed_task(claim)
        _kind, process = runtime_task_worker._DISPATCHED_TASKS[claim.id.hex]
        await asyncio.wait_for(process, 30)
        input_id = trigger_daemon._trigger_child_run_id(wrapper_id, effect='same_session', discriminator=str(session_id))
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            wrapper = await db.get(RuntimeTask, wrapper_id)
            assert wrapper.status == 'completed' and wrapper.metadata_json['delivery'] == 'same_session'
            row = await db.get(SessionTurnInput, input_id)
            assert row is not None
            admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == input_id))
            admission_id = admission.id
            budget_id = wrapper.budget_run_id
            assert budget_id is not None
        boundary = await runtime_task_worker.drain_direct_terminal_boundary_outbox_once(
            worker_id='remote-trigger-boundary', session_factory=app_user_sessionmaker)
        assert boundary['delivered'] == 1, boundary
        await asyncio.sleep(5.1)
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            result = await recover_admitted_session_inputs_once(db, worker_id='remote-trigger-input',
                tenant_id=seed['tenant'], admission_ids=(admission_id,))
            admission = await db.get(SessionInputAdmission, admission_id, populate_existing=True)
            assert result['dispatched'] == 1, (result, admission.dispatch_last_error)
            row = await db.get(SessionTurnInput, input_id, populate_existing=True)
            run_id = row.target_run_id
            task = await db.get(RuntimeTask, run_id)
            assert task.root_user_id == seed['owner'] and task.parent_session_id == str(session_id)
            assert task.budget_run_id == budget_id and task.root_runtime_task_id == wrapper_id
            claims = await RuntimeTaskClaimService(db=db, worker_id='remote-scheduled-leaf',
                task_types=('web_chat_turn',)).claim_available()
            claim = next(item for item in claims if item.id == run_id)
            # Invalidate after the durable claim, so the actual worker must
            # recheck current authority before the remote effect.
            if fault == 'budget_stopped':
                from app.models.runtime_budget import RuntimeBudgetRun

                (await db.get(RuntimeBudgetRun, budget_id)).status = 'stopped'
            elif fault == 'trigger_revoked':
                from app.models.trigger import AgentTrigger

                trigger = await db.get(AgentTrigger, UUID(saved.json()['id']))
                trigger.config = {**trigger.config, 'authority_state': 'quarantined'}
            elif fault == 'origin_tamper':
                metadata = deepcopy(task.metadata_json)
                metadata['remote_a2a']['trigger_origin']['wrapper_id'] = str(uuid4())
                task.metadata_json = metadata
            await db.commit()
        assert runtime_task_worker._dispatch_claimed_task(claim)
        _kind, process = runtime_task_worker._DISPATCHED_TASKS[claim.id.hex]
        await asyncio.wait_for(process, 30)
        if fault:
            assert not (receiver.directory / 'consumed-0.json').exists()
            async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
                task = await db.get(RuntimeTask, run_id)
                assert task.status == 'needs_reconciliation', task.metadata_json
                assert task.metadata_json['remote_a2a']['operations'][-1]['phase'] == 'not_started'
            view = await api.get(path + f'/tasks/{run_id}')
            assert view.status_code == 200 and 'abandon' in view.json()['recovery_actions'], view.text
            abandoned = await api.post(path + f'/tasks/{run_id}/operations', json={
                'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant']),
                'expected_version': 1, 'operation_id': str(uuid4()), 'action': 'abandon', 'params': {}})
            assert abandoned.status_code == 200 and abandoned.json()['local_abandoned'], abandoned.text
            assert not (receiver.directory / 'consumed-0.json').exists()
            return
        consumed = json.loads((receiver.directory / 'consumed-0.json').read_text())
        context = consumed['context']
        assert context['principal']['purpose'] == 'autonomous_agent' and context['principal']['autonomous'] is True
        assert context['budget_run_id'] == str(budget_id)
        assert 'REMOTE_SCHEDULED_ASSIGNMENT' in consumed['sdk_input']
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            assert (await db.get(RuntimeTask, run_id)).status == 'completed'
        returned = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='remote-trigger-return')
        assert returned['delivered'] >= 1, returned
        report = (receiver.directory / 'report-0.txt').read_text()
        transcript = await api.get(base + f'/sessions/{session_id}/transcript')
        assert transcript.status_code == 200 and any(item.get('content') == report for item in transcript.json())
