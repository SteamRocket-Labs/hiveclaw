"""R7a: canonical PostgreSQL history and the actual parent result reader."""

import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.kernel.engine import _dicts_to_llm_messages
from app.services.llm_client import LLMError
from tests.integration.test_a4c_one_item_withheld import _bindings, _FakeDB
from tests.integration.test_a4c_r2 import patch_validators
from tests.services.test_r7a_provider_contract import _builder, _calls, assert_tool_groups
from tests.services import test_session_semantic_history as history

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("restart", [False, True])
async def test_round_one_payload_rebuilt_from_committed_parallel_history(owner_sessionmaker, monkeypatch, restart):
    from app.services import session_model_round as rounds
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.web_chat_runtime import conversation_from_history_messages

    tenant, user, agent, session = await history._seed(owner_sessionmaker)
    _, run, turn = await history._submit_turn(owner_sessionmaker, user_id=user, agent_id=agent,
        session_id=session, content="Inspect three sources")
    await history._bind_round_one(owner_sessionmaker, tenant_id=tenant, agent_id=agent,
        session_id=session, run_id=run, turn_id=turn)
    calls = _calls(["search_memory", "grep_search", "list_files"])
    async with owner_sessionmaker() as db:
        request_id = await history._prepare_round(db, tenant_id=tenant, agent_id=agent,
            session_id=session, run_id=run, turn_id=turn, round_index=1)
        for index, call in enumerate(calls):
            await history._settle_tool_invocation(db, tenant_id=tenant, agent_id=agent,
                session_id=session, run_id=run, provider_request_id=request_id,
                provider_tool_use_id=call["id"], tool_name=call["function"]["name"],
                arguments={"index": index}, result_content=f"observation {index}",
                outcome="failed" if index == 1 else "success")
        await history._seal_round(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, turn_id=turn, round_index=1, provider_request_id=request_id,
            content="I will check these sources", tool_calls=calls,
            response_overrides={"reasoning_content": "Persisted reasoning"})
        if not restart:
            final_id = await history._prepare_round(db, tenant_id=tenant, agent_id=agent,
                session_id=session, run_id=run, turn_id=turn, round_index=2)
            seal = await history._seal_round(db, tenant_id=tenant, agent_id=agent, session_id=session,
                run_id=run, turn_id=turn, round_index=2, provider_request_id=final_id, content="Finished")
            await history._terminate_run(db, tenant_id=tenant, agent_id=agent, session_id=session,
                run_id=run, turn_id=turn, seal=seal)
        await db.commit()

    # A fresh DB session reads only durable canonical events/seals, with no
    # in-memory provider round. Same-run replay and new-run history share it.
    async with owner_sessionmaker() as db:
        recovered = await load_session_semantic_history(db, tenant_id=tenant, agent_id=agent, session_id=session)
    assert recovered.receipt["held_items"] == []
    conversation = conversation_from_history_messages(recovered.messages)
    replies = [entry["tool_call_id"] for entry in conversation if entry["role"] == "tool"]
    # Canonical rows share the round's sequence and may order siblings by
    # invocation identity. The protocol requires one reply per id, not a
    # particular sibling permutation.
    assert len(replies) == len(calls) and set(replies) == {call["id"] for call in calls}
    assert any(entry.get("reasoning_content") == "Persisted reasoning" for entry in conversation)

    source = {"source_kind": "explicit_memory", "source_id": "withdrawn", "source_version": "v1"}
    messages = _dicts_to_llm_messages([{"role": "system", "content": "Runtime"}, *conversation,
        {"role": "user", "content": "Continue"}])
    blocked_id = replies[0]
    blocked_content = next(message.content for message in messages if message.role == "tool" and message.tool_call_id == blocked_id)
    bindings = _bindings(len(messages), {index: [source] for index, message in enumerate(messages)
        if message.role == "tool" and message.tool_call_id == blocked_id})
    patch_validators(monkeypatch, [source])
    monkeypatch.setattr("app.services.content_input_receipts._validate_input_dependencies", AsyncMock())
    monkeypatch.setattr(rounds, "_bound_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(rounds, "append_session_events", AsyncMock())
    monkeypatch.setattr("app.services.session_model_inputs.capture_context_message_origins", AsyncMock())
    monkeypatch.setattr("app.services.session_model_inputs.build_model_input_bindings", AsyncMock(return_value=bindings))
    await rounds.prepare_model_request(_FakeDB(), tenant_id=tenant, agent_id=agent,
        session_id=session, run_id=run if restart else uuid4(), turn_id="rebuilt", round_index=1,
        messages=messages, tools=[], provider="local", model="local")
    for provider in ("openai", "anthropic"):
        payload = _builder(provider)._build_payload(messages, None, 0.7, 2048)
        assert_tool_groups(payload, provider)
        assert blocked_content not in json.dumps(payload)
        for index in range(3):
            if f"observation {index}" != blocked_content:
                assert f"observation {index}" in json.dumps(payload)


async def test_delegated_provider_400_is_recorded_and_read_by_parent_as_not_retryable(owner_sessionmaker, monkeypatch, tmp_path):
    from app.database import tenant_scoped_session
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services import web_chat_run_orchestrator as orchestrator, web_chat_runtime as runtime
    from app.agents import orchestrator as delegation
    from app.services.runtime_result_store import runtime_result_ref
    from app.tools.runtime import ToolExecutionRequest, ToolExecutionContext
    from app.tools.handlers import context_resources
    from tests.services.test_runtime_result_fanin_pg import _seed_parent
    from tests.kernel.test_parallel_tool_batch import _make_deps, _make_model
    from tests.services.test_web_chat_run_orchestrator import _failure_finalize_harness

    tenant, user, agent, session = await _seed_parent(owner_sessionmaker)
    run = uuid4()
    async with owner_sessionmaker() as db:
        db.add(RuntimeTask(id=run, tenant_id=tenant, parent_agent_id=agent, root_user_id=user,
            parent_session_id=str(session), task_type="delegation", status="running", metadata_json={}))
        await db.commit()

    class Client:
        async def stream(self, **kwargs):
            raise LLMError("HTTP 400: invalid params, tool call result does not follow tool call (2013)",
                http_status=400, delivery_state="rejected")

        async def close(self):
            pass

    result = await AgentKernel(_make_deps(create_client=lambda _: Client())).handle(InvocationRequest(
        model=_make_model(), messages=[{"role": "user", "content": "Do delegated work"}],
        agent_name="Child", role_description=""))
    calls = []
    await orchestrator._finalize_assistant_response(_failure_finalize_harness(calls), result,
        result.content, None, "failed", {"terminal_reason": "provider_error"})
    finalized = calls[0]
    # Real transcript persistence and outbox publication consume exactly the
    # finalizer arguments, so the emitted failure and parent body cannot diverge.
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run)
        task.status, task.result_summary, task.metadata_json = "failed", finalized["result_summary"], finalized["metadata_json"]
        await runtime._append_web_chat_runtime_failure_event(db, tenant_id=tenant, agent_id=agent,
            session_id=str(session), run_uuid=run, turn_id="r7a", status="failed", failure=finalized["failure"])
        request = delegation.AgentDelegationRequest(target=NS(id=agent, name="Child", tenant_id=tenant),
            target_model=None, conversation_messages=[], owner_id=user, tenant_id=tenant,
            session_id=str(session), parent_session_id=str(session), parent_agent_id=agent)
        outbox_id = await delegation._wake_parent_session_from_delegation_completion(db=db,
            request=request, task_id=str(run), status="failed", summary=task.result_summary)
        await db.commit()
        event = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == run,
            ChatTranscriptEvent.event_type == "runtime_failure.recorded"))
        assert event.metadata_json["v2_payload"]["retryable"] is False
        assert event.metadata_json["v2_payload"]["failure_code"] == "provider_bad_request"
        outbox = await db.get(RuntimeNotificationOutbox, outbox_id)
        assert outbox.terminal_status == "failed"
        ref = runtime_result_ref(result_id=outbox.result_object_id, sha256=outbox.result_sha256)
    monkeypatch.setattr(context_resources, "async_session", owner_sessionmaker)
    parent = json.loads(await context_resources.read_runtime_result(ToolExecutionRequest(tool_name="read_runtime_result",
        arguments={"result_ref": ref, "offset": 0, "limit": 12000}, context=ToolExecutionContext(
            agent_id=agent, user_id=user, tenant_id=str(tenant), session_id=str(session), workspace=tmp_path))))
    assert parent["status"] == "ok", parent
    body = json.loads(parent["content"])
    assert body["metadata"]["error_code"] == "provider_bad_request"
    assert body["metadata"]["retryable"] is False
