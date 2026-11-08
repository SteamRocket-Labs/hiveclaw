"""Canonical settled-result fixture: real PostgreSQL/input rounds, synthetic stdout."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
    _admitted_write_messages, _prepare_write, native_workspace,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('tool', ['run_command', 'execute_code'])
async def test_unavailable_skill_pointer_does_not_withhold_nonfile_result(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tool,
):
    from app.kernel.engine import _dicts_to_llm_messages, _llm_messages_to_dicts
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services import content_input_receipts
    from app.services.content_input_receipts import ContentInputReceiptError, persist_workspace_execution_evidence
    from app.services.session_model_inputs import _hash, build_model_input_bindings
    from app.services.session_model_round import prepare_model_request
    from app.services.session_semantic_history import _event_payload, load_session_semantic_history
    from app.services.session_tool_runtime import complete_tool_invocation, mark_tool_effect_started
    from app.services.web_chat_runtime import conversation_from_history_messages
    from app.services.workspace_resource_authority import WorkspaceExecutionInputs

    n = native_workspace
    args = {'command': 'printf fixture'} if tool == 'run_command' else {'code': "print('fixture')", 'language': 'python'}
    body = f'SYNTHETIC_SETTLED_{tool.upper()}_RESULT'
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        assert tuple((await db.execute(text('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user'))).one()) == (False, False)
        invocation, _, turn = await _prepare_write(db, n, tool_name=tool, arguments=args,
            messages=await _admitted_write_messages(db, n))
        await mark_tool_effect_started(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            invocation_id=invocation.id, effective_arguments=args)
        await db.commit()
    context = await n.service.runtime_resolver.resolve(agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run))
    inputs = WorkspaceExecutionInputs()
    inputs.observe({}, context.workspace_authority_scope)
    context.workspace_authority_scope = replace(context.workspace_authority_scope, execution_inputs=inputs)
    captured = await persist_workspace_execution_evidence(context=context, tool_name=tool,
        tool_call_id=invocation.provider_tool_use_id, arguments=args, result=body, states={}, lineage=[], errors={})
    assert captured['status'] == 'captured', captured

    async def unavailable_skill_capture(*_args, **_kwargs):
        raise ContentInputReceiptError('workspace_execution_evidence_unavailable', 'Injected Skill evidence failure.')

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        with monkeypatch.context() as patch:
            patch.setattr(content_input_receipts, 'load_skill_input_evidence', unavailable_skill_capture)
            await complete_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
                invocation_id=invocation.id, provider_result_content=body, execution_evidence={
                    'schema': 'hive.tool_execution_evidence.v1', 'status': 'settled', 'retryable': False,
                    'tool_decision': {'schema': 'hive.tool_decision.v1', 'decision_id': 'fixture-result',
                        'outcome': 'allow', 'input_hash': invocation.args_hash,
                        'policy_snapshot_hash': 'a' * 64, 'capability_snapshot_hash': 'b' * 64},
                    'execution_frame': {'status': 'completed', 'output_hash': _hash(body)},
                })
        row = await db.get(SessionToolInvocation, invocation.id, populate_existing=True)
        assert row.effect_state == 'effect_committed'
        event = await db.get(ChatTranscriptEvent, row.result_event_id)
        assert _event_payload(event)['metadata']['skill_input_receipt'] == {
            'status': 'unavailable', 'reason_code': 'workspace_execution_evidence_unavailable'}
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
        messages = _dicts_to_llm_messages(conversation_from_history_messages(history.messages))
        binding = await build_model_input_bindings(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            messages=messages, wire_messages=_llm_messages_to_dicts(messages))
        entry = next(e for e in binding['messages'] if e.get('source_ref') == f'session-tool-result:{invocation.id}')
        assert entry['dependency_status'] == 'resolved', entry
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=2, messages=messages, tools=[], provider='openai', model='synthetic-no-provider')
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        reads = [m for m in result.model_request_snapshot_json['wire_request']['messages']
            if m.get('role') == 'tool' and m.get('tool_call_id') == invocation.provider_tool_use_id]
        assert len(reads) == 1 and reads[0]['content'] == body, result.model_request_snapshot_json
        await db.commit()
