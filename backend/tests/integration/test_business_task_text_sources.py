"""Business task delivery consumes the current authority of original inputs."""

import json
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from tests.integration.test_m4v_upload_intents_postgres import original_api, provider, delivered, s3_server  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run, ScriptedLLMClient

pytestmark = pytest.mark.usefixtures('migrated_pg_url')
RESULT = 'BUSINESS_RESULT_DERIVED_FROM_PRIVATE_ORIGINAL'


@pytest.mark.parametrize('provider', ['filesystem'], indirect=True)
@pytest.mark.parametrize('withdrawn,unknown_effect', [(False, False), (True, False), (False, True)])
async def test_business_task_text_rechecks_its_actual_original_source(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, withdrawn, unknown_effect,  # noqa: F811
):
    import app.database as database
    import app.runtime.invoker as invoker
    from app.api import tasks, upload
    from app.config import get_settings
    from app.core.security import create_access_token
    from app.core.source_read_purpose import source_recipient_read
    from app.models.agent import Agent
    from app.models.channel_config import ChannelConfig
    from app.models.chat_session import ChatSession
    from app.models.llm import LLMModel
    from app.models.plan_request import AgentPlanRequest
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.models.task import Task
    from app.models.user import User
    from app.schemas.schemas import TaskCreate
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.external_principal_service import bind_authenticated_self_channel_principal
    from app.services.feishu_service import FeishuService
    from app.services.plan_authorization_lease import canonical_action_artifact, issue_plan_authorization_leases
    from app.services.plan_mode_core import compute_plan_hash
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_task_worker import _execute_claimed_business_task
    from app.services.secrets_provider import get_secrets_provider

    _, client = original_api
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    monkeypatch.setattr(get_settings(), 'AGENT_DATA_DIR', str(seed['data_root']))
    monkeypatch.setattr(upload, 'WORKSPACE_ROOT', seed['data_root'])
    for module, name in ((agent_tools, 'WORKSPACE_ROOT'), (agent_context, 'PERSISTENT_DATA'),
        (agent_context, 'TOOL_WORKSPACE')):
        monkeypatch.setattr(module, name, seed['data_root'])
    monkeypatch.setattr('app.tools.workspace.WORKSPACE_ROOT', seed['data_root'])
    monkeypatch.setattr(agent_tools, '_TOOL_RUNTIME_SERVICE', None)
    monkeypatch.setattr(agent_manager, 'settings', get_settings())
    (seed['agent_root'] / 'workspace').rmdir()
    seed['agent_root'].rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed['agent_id'])
        await AgentManager().initialize_agent_files(db, agent, personality='Read authorized originals for business tasks.',
            original_author_id=seed['user_id'])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt('synthetic-business-task-provider-only')
        model.base_url, model.max_input_tokens = 'http://127.0.0.1:1', 256_000
        await db.commit()
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, '__name__', '').startswith('app.'):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)

    async def no_wakeup(**_kwargs):
        pass

    monkeypatch.setattr(tasks, 'notify_runtime_task_worker', no_wakeup)
    client._transport.app.include_router(tasks.router)
    headers = {'Authorization': 'Bearer ' + create_access_token(str(seed['user_id']), 'member', str(seed['tenant_id']))}
    uploaded = await client.post('/chat/upload', headers=headers, files={'file': ('source.txt', b'PRIVATE_BUSINESS_INPUT')},
        data={'agent_id': str(seed['agent_id']), 'skip_personal_kb': 'true'})
    assert uploaded.status_code == 200, uploaded.text
    original = uploaded.json()
    original_id = uuid.UUID(original['original_attachment']['id'])
    body = TaskCreate(request_id=str(uuid.uuid4()), title='Original-based report',
        description='Read the authorized original and deliver the complete report.',
        confirmed_plan_session_id=str(seed['session_id'])).model_dump(mode='json')
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        config = ChannelConfig(id=uuid.uuid4(), tenant_id=seed['tenant_id'], agent_id=seed['agent_id'], channel_type='feishu',
            app_id='synthetic-business-result', app_secret='synthetic-channel-only', is_configured=True)
        db.add(config)
        await db.flush()
        bound = await bind_authenticated_self_channel_principal(db, tenant_id=seed['tenant_id'], config=config,
            provider_subject_id='ou-business-owner', user_id=seed['user_id'], actor_user_id=seed['user_id'])
        session = await db.get(ChatSession, seed['session_id'])
        session.delivery_target_json = {'channel': 'feishu', 'receive_id': 'ou-business-owner', 'receive_id_type': 'open_id',
            'chat_type': 'p2p', 'session_id': str(session.id), 'external_principal_id': str(bound.principal.id)}
        plan_json = {'schema': 'hive_plan.v1', 'intent_type': 'in_session_execution', 'title': body['title'],
            'objective': body['description'], 'steps': [{'order': 1, 'description': body['description']}],
            'success_criteria': ['Deliver the complete report.'], 'stop_conditions': ['The owner cancels.'],
            'handoff': {'target': 'continue_current_session'}, 'authorization_scopes': [{'action_kind': 'start_long_task',
                'target_ref': 'task:new', 'arguments': canonical_action_artifact(body), 'summary': body['description']}]}
        plan = AgentPlanRequest(tenant_id=seed['tenant_id'], agent_id=seed['agent_id'], session_id=str(session.id),
            requested_by_user_id=seed['user_id'], confirmed_by_user_id=seed['user_id'], confirmed_at=datetime.now(UTC),
            source='web_chat', intent_type='in_session_execution', original_request=body['description'], status='confirmed',
            plan_version=1, plan_hash=compute_plan_hash(plan_json), plan_json=plan_json)
        db.add(plan)
        await db.flush()
        leases = await issue_plan_authorization_leases(db=db, plan=plan, confirming_user_id=seed['user_id'])
        assert len(leases) == 1
        body.update(confirmed_plan_id=str(plan.id), confirmed_plan_version=1, confirmed_plan_hash=plan.plan_hash)
        await db.commit()
    created = await client.post(f"/agents/{seed['agent_id']}/tasks/", headers=headers, json=body)
    assert created.status_code == 201, created.text
    run_id = uuid.UUID(created.json()['active_runtime_task_id'])
    repeated = await client.post(f"/agents/{seed['agent_id']}/tasks/", headers=headers, json=body)
    assert repeated.status_code == 201 and repeated.json()['id'] == created.json()['id']
    assert repeated.json()['active_runtime_task_id'] == str(run_id)
    (seed['agent_root'] / original['workspace_path']).unlink()
    model_client = ScriptedLLMClient([{'tool_calls': [{'id': 'business-original-read', 'type': 'function', 'function': {
        'name': 'fs_read', 'arguments': json.dumps({'path': original['workspace_path'], 'original_attachment_id': str(original_id)})}}],
        'finish_reason': 'tool_calls'}, {'content': RESULT, 'finish_reason': 'stop'}])
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: model_client)
    if unknown_effect:
        from app.kernel.contracts import ToolLifecyclePersistenceError
        from app.services.delegation_session_runtime import DelegationSessionRuntime

        persist = DelegationSessionRuntime.tool_call

        async def lose_result_receipt(self, data):
            if data.get('status') in {'done', 'completed'}:
                raise ToolLifecyclePersistenceError(tool_name=data['name'],
                    provider_tool_use_id=data['tool_call_id'], lifecycle=data['status'])
            return await persist(self, data)

        monkeypatch.setattr(DelegationSessionRuntime, 'tool_call', lose_result_receipt)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        run = await db.get(RuntimeTask, run_id)
        run.status, run.claimed_by, run.claim_version, run.attempt_count = 'running', 'native-business-source', 1, 1
        run.claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        await db.commit()
    fence = set_runtime_task_fence(task_id=run_id, worker_id='native-business-source', claim_version=1)
    try:
        await _execute_claimed_business_task(run_id)
    finally:
        reset_runtime_task_fence(fence)
    (tmp_path / 'business-model-requests.json').write_text(json.dumps(model_client.requests, default=str))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        run = await db.get(RuntimeTask, run_id)
        (tmp_path / 'business-runtime-result.json').write_text(json.dumps({
            'status': run.status, 'child_session_id': run.child_session_id, 'metadata': run.metadata_json}, default=str))
        if unknown_effect:
            assert run.status == 'needs_reconciliation', run.metadata_json
            task = await db.get(Task, uuid.UUID(created.json()['id']))
            assert task.last_execution_status == 'needs_reconciliation'
            tool = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == run_id))
            assert tool.effect_state in {'effect_started', 'needs_reconciliation'}
            assert len(model_client.requests) == 1
    if unknown_effect:
        detail = await client.get(f"/agents/{seed['agent_id']}/tasks/{created.json()['id']}", headers=headers)
        assert detail.status_code == 200 and detail.json()['task']['actions']['can_reconcile']
        assert not detail.json()['task']['actions']['can_retry']
        await _execute_claimed_business_task(run_id)
        assert len(model_client.requests) == 1
        revoked = await client.post(f'/chat/attachments/{original_id}/withdraw', headers=headers,
            json={'expected_content_sha256': original['original_attachment']['content_sha256']})
        assert revoked.status_code == 200, revoked.text
        calls = []

        async def status_transport(self, _app_id, _secret, recipient, *args, **kwargs):
            calls.append({'recipient': recipient, 'args': args, 'kwargs': kwargs})
            return {'data': {'message_id': 'synthetic-business-status-ack'}}

        monkeypatch.setattr(FeishuService, 'send_message', status_transport)
        service = ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker)
        counts = await service.drain_once(worker_id='business-status')
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            row = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == run_id))
            observed = {'calls': calls, 'counts': counts, 'status': row.status, 'last_error': row.last_error}
            (tmp_path / 'business-status-source.json').write_text(json.dumps(observed, indent=2))
            assert len(calls) == 1 and row.status == 'delivered', observed
            assert calls[0]['recipient'] == 'ou-business-owner'
            assert 'reconciliation' in json.dumps(calls)
            assert not any(value in json.dumps(calls) for value in ('PRIVATE_BUSINESS_INPUT', RESULT, body['title']))
            from app.services.channel_delivery_outbox import _claimed

            saved = _claimed(row)
            for changed in (
                replace(saved, text='PRIVATE_BUSINESS_INPUT'),
                replace(saved, artifact_ids=(uuid.uuid4(),)),
                replace(saved, user_id=uuid.uuid4()),
                replace(saved, metadata={**saved.metadata, 'business_task_id': str(uuid.uuid4())}),
            ):
                with pytest.raises(ContentInputReceiptError) as denied:
                    await service._authorize_native_result_text(db, changed)
                assert denied.value.status_code == 403
            with pytest.raises(ContentInputReceiptError) as no_result:
                await service._authorize_native_result_text(db,
                    replace(saved, metadata={**saved.metadata, 'projection_kind': 'result'}))
            assert no_result.value.status_code == 503
            user = await db.get(User, seed['user_id'])
            user.is_active = False
            await db.flush()
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as inactive:
                await service._authorize_native_result_text(db, saved)
            assert inactive.value.status_code == 403
        assert len(calls) == 1
        assert len(model_client.requests) == 1
        return
    assert len(model_client.requests) == 2 and any(message.get('role') == 'tool' and 'PRIVATE_BUSINESS_INPUT' in str(message)
        for message in model_client.requests[-1]['messages'])
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        run = await db.get(RuntimeTask, run_id)
        assert run.status == 'completed', run.metadata_json
        row = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == run_id))
        assert row is not None and row.metadata_json['source'] == 'business_task_runtime' and RESULT in row.text_content
        outbox_id = row.id
        user = await db.get(User, seed['user_id'])
        dependencies = [{'source_kind': 'original_attachment', 'source_id': str(original_id),
            'source_version': original['original_attachment']['content_sha256']}]
        with source_recipient_read(session_id=seed['session_id'], user_id=user.id):
            await _validate_input_dependencies(db, seed['tenant_id'], dependencies, user=user)
    if withdrawn:
        revoked = await client.post(f'/chat/attachments/{original_id}/withdraw', headers=headers,
            json={'expected_content_sha256': original['original_attachment']['content_sha256']})
        assert revoked.status_code == 200, revoked.text
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            with source_recipient_read(session_id=seed['session_id'], user_id=user.id):
                with pytest.raises(ContentInputReceiptError) as oracle:
                    await _validate_input_dependencies(db, seed['tenant_id'], dependencies, user=user)
            assert oracle.value.status_code == 403
    calls = []

    async def text_transport(self, _app_id, _secret, recipient, *args, **kwargs):
        calls.append({'recipient': recipient, 'args': args, 'kwargs': kwargs})
        return {'data': {'message_id': 'synthetic-business-ack'}}

    monkeypatch.setattr(FeishuService, 'send_message', text_transport)
    counts = await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='business-source')
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        row = await db.get(ChannelDeliveryOutbox, outbox_id)
        observed = {'withdrawn': withdrawn, 'calls': calls, 'counts': counts, 'status': row.status, 'last_error': row.last_error}
        (tmp_path / 'business-text-source.json').write_text(json.dumps(observed, indent=2))
        assert len(calls) == int(not withdrawn), observed
        assert row.status == ('dead_letter' if withdrawn else 'delivered'), observed
        if not withdrawn:
            assert RESULT in json.dumps(calls)
