"""A real tool question binds an explicit admitted answer to the original work."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation, SessionTurnInput
from tests.services.test_session_input_control_v2 import _mark_web_terminal_boundary_delivered, _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _ask(db, *, tenant_id, agent_id, session_id, run_id, service, user_id, arguments=None):
    from app.services.session_model_round import prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_tool_runtime import prepare_tool_invocation, complete_tool_invocation
    from app.services.session_permission_runtime import _execution_evidence

    turn_id, call_id = f"turn-{run_id.hex}", f"question-{run_id.hex}"
    arguments = arguments or {"questions": [{"question": "Which audience?", "header": "Audience",
                               "options": [{"label": "Team"}, {"label": "Personal"}]}]}
    wire = {"messages": [{"role": "user", "content": "Prepare a comparison"}],
            "tools": [{"type": "function", "function": {"name": "ask_user_question"}}], "max_tokens": 8192}
    request_id = await prepare_model_request(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
        run_id=run_id, turn_id=turn_id, round_index=1, continuation_index=0, messages=wire["messages"], tools=wire["tools"],
        provider="openai", model="synthetic-no-network", wire_request=wire, attempt_owner="question-test")
    await seal_model_response(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id, run_id=run_id,
        turn_id=turn_id, round_index=1, provider_request_id=request_id, response={"content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": "ask_user_question", "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls",
            "usage": {"prompt_tokens": 20, "completion_tokens": 5}})
    await commit_sealed_model_round(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id, run_id=run_id,
        turn_id=turn_id, round_index=1, provider_request_id=request_id)
    invocation = await prepare_tool_invocation(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
        run_id=run_id, provider_request_id=request_id, provider_tool_use_id=call_id, tool_name="ask_user_question", arguments=arguments)
    await db.commit()
    trace = {}
    result = await service.execute("ask_user_question", arguments, agent_id=agent_id, user_id=user_id,
        session_id=str(session_id), runtime_task_id=str(run_id), turn_id=turn_id, tool_call_id=call_id,
        emit_runtime_hooks=False, trace_metadata_sink=trace)
    events = await complete_tool_invocation(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
        invocation_id=invocation.id, provider_result_content=str(result), execution_evidence=_execution_evidence(trace),
        effective_arguments=trace["effective_arguments"])
    await db.commit()
    questions = [event for event in events if event.item_kind == "user_question"]
    assert len(questions) == 1
    assert questions[0].metadata_json["v2_payload"]["requested_by_user_id"] == str(user_id)
    assert (await db.get(SessionToolInvocation, invocation.id)).result_event_id == events[-1].id
    return questions[0].item_id


@pytest.mark.parametrize("status", ["running", "completed", "legacy", "transformed", "transformed_active", "wrong_requester"])
async def test_tool_question_accepts_one_explicit_answer_and_continues_original_work(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, status,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api.chat_sessions import router
    from app.core.security import get_current_user
    from app.database import get_db
    from app.models.user import User
    from app.services import agent_tools
    from app.services.session_user_question import answer_projection

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)

    async def no_wakeup(**_kwargs):
        pass

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    hook_calls = []
    if status in {"transformed", "transformed_active"}:
        from app.runtime.hooks import HookResult

        async def hook(event, **_kwargs):
            if event.value == "elicitation_result":
                hook_calls.append(event.value)
                return HookResult(elicitation_action="accept", elicitation_content={"answer": "Audience: Authorized team"})
            return HookResult()

        monkeypatch.setattr("app.runtime.hooks.emit_hook", hook)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        if status == "legacy":
            question_id = uuid.uuid4()
            db.add(ChatTranscriptEvent(id=question_id, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
                run_id=run_id, sequence=1, event_type="tool_result", actor_type="tool", visibility_scope="direct_user",
                listed_surface="chat", content=json.dumps({"name": "ask_user_question", "result": {
                    "status": "awaiting_user_clarification", "questions": [{"question": "Which audience?"}], "blocking": True}}),
                metadata_json={"tool_name": "ask_user_question"}))
        else:
            question_id = await _ask(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id, run_id=run_id,
                                     service=agent_tools._get_tool_runtime_service(), user_id=user_id)
        user = await db.get(User, user_id)
        task = await db.get(RuntimeTask, run_id)
        task.status = "running" if status in {"running", "wrong_requester", "transformed_active"} else "completed"
        if status == "wrong_requester":
            other = User(id=uuid.uuid4(), tenant_id=tenant_id, username=f"other-{uuid.uuid4().hex}",
                email=f"{uuid.uuid4().hex}@question.test", display_name="Other requester", password_hash="synthetic")
            db.add(other)
            await db.flush()
            task.root_user_id = other.id
        if task.status == "completed":
            await _mark_web_terminal_boundary_delivered(db, task=task, agent_id=agent_id, session_id=session_id)
        await db.commit()

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            yield db

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = scoped_db
    input_id = uuid.uuid4()
    body = {"kind": "answer_request", "input_id": str(input_id), "idempotency_key": f"answer:{input_id}",
            "request_item_id": str(question_id), "content_parts": [{"type": "text", "text": "Audience: Team"}],
            "terminal_fallback": "queue_next_turn"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://question-test") as client:
        if status == "running":
            missing = await client.post(f"/api/agents/{agent_id}/sessions/{session_id}/inputs", json={
                **body, "request_item_id": str(uuid.uuid4())})
            assert missing.status_code == 409 and missing.json()["detail"]["code"] == "user_question_not_found"
        first = await client.post(f"/api/agents/{agent_id}/sessions/{session_id}/inputs", json=body)
        assert first.status_code == 200, first.text
        if status == "wrong_requester":
            assert first.json()["status"] == "rejected"
            assert first.json()["dispatch"]["reason_code"] == "user_question_requester_mismatch"
            async with owner_sessionmaker() as db:
                latest = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.item_id == question_id)
                    .order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
                assert latest.lifecycle == "waiting"
                assert len((await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == tenant_id))).all()) == 1
            return
        assert first.json()["status"] == ("queued" if status in {"running", "transformed_active"} else "rolled_over"), first.text
        replay = await client.post(f"/api/agents/{agent_id}/sessions/{session_id}/inputs", json=body)
        assert replay.status_code == 200 and replay.json()["replayed"] is True, replay.text
        if status == "running":
            revised_parts = [{"type": "text", "text": "Audience: Personal"}]
            revised = await client.patch(f"/api/agents/{agent_id}/sessions/{session_id}/inputs/{input_id}",
                json={"content_parts": revised_parts}, headers={"If-Match": "1"})
            assert revised.status_code == 200 and revised.json()["status"] == "queued", revised.text
            body["content_parts"] = revised_parts
        other_id = uuid.uuid4()
        late = await client.post(f"/api/agents/{agent_id}/sessions/{session_id}/inputs", json={
            **body, "input_id": str(other_id), "idempotency_key": f"answer:{other_id}"})
        assert late.status_code == 200 and late.json()["status"] == "rejected", late.text
        if status == "running":
            cancelled = await client.delete(f"/api/agents/{agent_id}/sessions/{session_id}/inputs/{input_id}",
                                             headers={"If-Match": "2"})
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
            async with owner_sessionmaker() as db:
                reopened = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.item_kind == "user_question").order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
                assert reopened.lifecycle == "waiting" and reopened.item_id != question_id
                assert reopened.metadata_json["v2_payload"]["supersedes_question_id"] == str(question_id)
            new_answer_id = uuid.uuid4()
            answered_again = await client.post(f"/api/agents/{agent_id}/sessions/{session_id}/inputs", json={
                **body, "input_id": str(new_answer_id), "idempotency_key": f"answer:{new_answer_id}",
                "request_item_id": str(reopened.item_id)})
            assert answered_again.status_code == 200 and answered_again.json()["status"] == "queued", answered_again.text
    async with owner_sessionmaker() as db:
        question = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.item_kind == "user_question",
            ChatTranscriptEvent.input_id == input_id, ChatTranscriptEvent.lifecycle == "completed")
            .order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
        assert question.input_id == input_id and question.metadata_json["actor"]["id"] == str(user_id)
        row = await db.get(SessionTurnInput, input_id)
        assert row.intent == "answer_request" and row.content_parts_json == body["content_parts"]
        expected = "Audience: Authorized team" if status in {"transformed", "transformed_active"} else body["content_parts"][0]["text"]
        assert await answer_projection(db, row) == expected
        tasks = (await db.scalars(select(RuntimeTask).where(RuntimeTask.parent_session_id == str(session_id)))).all()
        assert len(tasks) == (1 if status in {"running", "transformed_active"} else 2)
        if status not in {"running", "transformed_active"}:
            successor = await db.get(RuntimeTask, uuid.uuid5(input_id, "session-v2-successor-run"))
            assert successor.prompt == expected and successor.root_user_id == user_id
            assert successor.parent_session_id == str(session_id)
        if status in {"transformed", "transformed_active"}:
            assert hook_calls == ["elicitation_result"]
    if status == "transformed_active":
        from app.services.session_model_round import bind_round_inputs

        for replay in (False, True):
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, tenant_id)
                messages = await bind_round_inputs(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
                    run_id=run_id, turn_id=f"turn-{run_id.hex}", round_index=2, bind_new_inputs=not replay)
                assert [message["content"] for message in messages] == ["Audience: Authorized team"]
                assert (await db.get(SessionTurnInput, input_id)).content_parts_json == body["content_parts"]
                await db.commit()
