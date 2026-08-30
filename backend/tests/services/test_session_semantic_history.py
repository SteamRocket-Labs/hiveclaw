"""SESSION-CONTEXT-001 regression: same-Session provider history must come from
the canonical transcript.

Session V2 turns persist provider-history semantics in ``ChatTranscriptEvent``
(``human_input.accepted``, ``assistant_text.snapshot``,
``tool_call.started``/``tool_result.completed``), not the legacy read model.
Artifact-bearing tool results may materialize a ``ChatMessage`` compatibility
anchor solely to satisfy the existing ``ChatArtifact.message_id`` FK and mixed-
plane UI projection; that row never becomes provider-history authority. A later
turn that assembled its conversation from the legacy read model would therefore
still lose or distort canonical history.

These tests drive the real Session V2 ingress/persistence services against real
PostgreSQL (``submit_live_human_input`` admission + dispatch, real model-round
sealing, real tool invocation lifecycle, real canonical terminal outcome), then
load history through the live ``web_chat_runtime._load_runtime_context`` entry
and convert it with the provider-conversation seam, asserting the exact
contract the next provider request must receive.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed(owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, agent_id, session_id = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Semantic History Tenant", slug=f"semantic-history-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"semantic-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@semantic-history.test",
                password_hash="x",
                display_name="Semantic History",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Semantic History Agent", creator_id=user_id))
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                session_kind="human_chat",
                runtime_source="web_chat",
            )
        )
        await db.commit()
    return tenant_id, user_id, agent_id, session_id


async def _submit_turn(
    owner_sessionmaker,
    *,
    user_id,
    agent_id,
    session_id,
    content,
):
    """Run the production live-input ingress: accept, hook-admit, queue, dispatch."""

    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.session_live_input import submit_live_human_input

    input_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        session = await db.get(ChatSession, session_id)
        assert agent is not None and user is not None and session is not None
        receipt = await submit_live_human_input(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content=content,
            source="web",
            input_id=input_id,
            idempotency_key=f"live:{input_id}",
        )
        await db.commit()
    assert receipt["admission_state"] == "admitted"
    run = receipt.get("run") or {}
    assert run.get("run_id"), f"live input did not dispatch a runtime run: {receipt}"
    return input_id, uuid.UUID(str(run["run_id"])), str(run["turn_id"])


async def _bind_round_one(
    owner_sessionmaker,
    *,
    tenant_id,
    agent_id,
    session_id,
    run_id,
    turn_id,
):
    from app.services.session_model_round import bind_round_inputs

    async with owner_sessionmaker() as db:
        await bind_round_inputs(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
        )
        await db.commit()


async def _prepare_round(
    db,
    *,
    tenant_id,
    agent_id,
    session_id,
    run_id,
    turn_id,
    round_index,
):
    from app.services.session_model_round import prepare_model_request

    return await prepare_model_request(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        round_index=round_index,
        messages=[{"role": "user", "content": "marker"}],
        tools=None,
        provider="openai",
        model="gpt-test",
        wire_request={"messages": [], "tools": []},
        attempt_owner="semantic-history-test",
    )


async def _seal_round(
    db,
    *,
    tenant_id,
    agent_id,
    session_id,
    run_id,
    turn_id,
    round_index,
    provider_request_id,
    content,
    tool_calls=None,
    response_overrides=None,
):
    from app.services.session_model_round import commit_model_response

    return await commit_model_response(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        round_index=round_index,
        provider_request_id=provider_request_id,
        response={
            "content": content,
            "tool_calls": tool_calls or [],
            "finish_reason": "stop",
            "usage": {},
            **dict(response_overrides or {}),
        },
    )


async def _settle_tool_invocation(
    db,
    *,
    tenant_id,
    agent_id,
    session_id,
    run_id,
    provider_request_id,
    provider_tool_use_id,
    tool_name,
    arguments,
    result_content=None,
):
    """Run the real tool lifecycle; without ``result_content`` the invocation
    stays started-but-unsettled (interrupted prior run shape)."""

    from app.services.session_tool_runtime import (
        complete_tool_invocation,
        mark_tool_effect_started,
        prepare_tool_invocation,
    )

    invocation = await prepare_tool_invocation(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        provider_request_id=provider_request_id,
        provider_tool_use_id=provider_tool_use_id,
        tool_name=tool_name,
        arguments=arguments,
    )
    if result_content is None:
        await db.flush()
        return invocation
    await mark_tool_effect_started(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        invocation_id=invocation.id,
    )
    await complete_tool_invocation(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        invocation_id=invocation.id,
        provider_result_content=result_content,
        execution_evidence={
            "schema": "hive.tool_execution_evidence.v1",
            "status": "settled",
            "retryable": False,
            "tool_decision": {
                "schema": "hive.tool_decision.v1",
                "decision_id": f"decision-{provider_tool_use_id}",
                "outcome": "allow",
                "input_hash": invocation.args_hash,
                "policy_snapshot_hash": "a" * 64,
                "capability_snapshot_hash": "b" * 64,
            },
            "execution_frame": {"status": "completed", "output_hash": "c" * 64},
        },
    )
    await db.flush()
    return invocation


async def _terminate_run(
    db,
    *,
    tenant_id,
    agent_id,
    session_id,
    run_id,
    turn_id,
    seal,
):
    """Finish a run through the real canonical terminal outcome transaction."""

    from app.services.session_terminal_outcome import commit_terminal_outcome, prepare_and_seal_run_outcome

    outcome = await prepare_and_seal_run_outcome(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        turn_id=turn_id,
        run_id=run_id,
        terminal_result_id=uuid.UUID(str(seal["result_id"])),
    )
    await commit_terminal_outcome(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        outcome_id=outcome.id,
    )
    await db.commit()


async def _run_v2_turn(
    owner_sessionmaker,
    *,
    tenant_id,
    user_id,
    agent_id,
    session_id,
    prompt,
    answer,
    tool=None,
):
    """Execute one full production-shaped V2 turn and return (run_id, turn_id)."""

    _input_id, run_id, turn_id = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content=prompt,
    )
    await _bind_round_one(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
    )
    async with owner_sessionmaker() as db:
        if tool is not None:
            request_id = await _prepare_round(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                round_index=1,
            )
            tool_use_id = tool["provider_tool_use_id"]
            await _settle_tool_invocation(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                provider_request_id=request_id,
                provider_tool_use_id=tool_use_id,
                tool_name=tool["tool_name"],
                arguments=tool["arguments"],
                result_content=tool.get("result_content"),
            )
            await _seal_round(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                round_index=1,
                provider_request_id=request_id,
                content="",
                tool_calls=[
                    {
                        "id": tool_use_id,
                        "type": "function",
                        "function": {
                            "name": tool["tool_name"],
                            "arguments": '{"path": "marker.txt"}',
                        },
                    }
                ],
            )
            final_request_id = await _prepare_round(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                round_index=2,
            )
            seal = await _seal_round(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                round_index=2,
                provider_request_id=final_request_id,
                content=answer,
            )
        else:
            request_id = await _prepare_round(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                round_index=1,
            )
            seal = await _seal_round(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                round_index=1,
                provider_request_id=request_id,
                content=answer,
            )
        await _terminate_run(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            seal=seal,
        )
    return run_id, turn_id


async def _set_run_status(owner_sessionmaker, *, run_id, status):
    from app.models.runtime_task import RuntimeTask

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        task.status = status
        await db.commit()


async def _load_history_via_live_entry(owner_sessionmaker, monkeypatch, *, run_id):
    """Call the live runtime-context entry and return its provider conversation."""

    import app.services.web_chat_runtime as web_chat_runtime
    from app.services.web_chat_runtime import conversation_from_history_messages

    monkeypatch.setattr(web_chat_runtime, "_async_session", owner_sessionmaker, raising=True)
    loaded = await web_chat_runtime._load_runtime_context(run_id)
    assert len(loaded) == 7
    runtime_task, _agent, _user, _model, _fallback, history_messages, _session = loaded
    conversation = conversation_from_history_messages(history_messages)
    return runtime_task, history_messages, conversation


def _role_content_pairs(conversation):
    return [(entry.get("role"), entry.get("content")) for entry in conversation]


async def test_second_turn_provider_conversation_receives_prior_turn_semantics(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.audit import ChatMessage

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    first_prompt = "WEEKEND-RC-P01-MAIN first marker prompt"
    first_answer = "first model-authored No-Go answer"
    second_prompt = "WEEKEND-RC-P02 audit your previous answer"

    await _run_v2_turn(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        prompt=first_prompt,
        answer=first_answer,
    )

    async with owner_sessionmaker() as db:
        materialized = list(
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.agent_id == agent_id,
                        ChatMessage.conversation_id == str(session_id),
                        ChatMessage.role.in_(("user", "assistant")),
                    )
                )
            ).scalars()
        )
    assert materialized == [], "production V2 terminal path must not materialize ChatMessage user/assistant rows"

    _input2, run2, _turn2 = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content=second_prompt,
    )

    _runtime_task, _history_messages, conversation = await _load_history_via_live_entry(
        owner_sessionmaker, monkeypatch, run_id=run2
    )

    pairs = _role_content_pairs(conversation)
    assert ("user", first_prompt) in pairs, f"prior user semantics missing from provider history: {pairs}"
    assert ("assistant", first_answer) in pairs, f"prior assistant semantics missing from provider history: {pairs}"
    assert pairs.index(("user", first_prompt)) < pairs.index(("assistant", first_answer)), (
        f"prior turn semantics out of order: {pairs}"
    )
    user_contents = [content for role, content in pairs if role == "user"]
    assert user_contents.count(first_prompt) == 1, f"prior user input duplicated: {pairs}"
    assert all(second_prompt != content for content in user_contents), (
        f"current-turn prompt leaked into history (it is injected separately per round): {pairs}"
    )
    roles = {role for role, _content in pairs}
    assert roles <= {"user", "assistant", "tool"}, f"system/debug projections leaked as conversation: {pairs}"


async def test_prior_turn_tool_semantics_replay_in_provider_history(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    first_prompt = "read the marker file and decide"
    tool_use_id = f"tool-use-{uuid.uuid4().hex}"
    tool_result_content = "marker file bytes: WEEKEND-RC"
    final_answer = "decision after reading the marker file"

    await _run_v2_turn(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        prompt=first_prompt,
        answer=final_answer,
        tool={
            "provider_tool_use_id": tool_use_id,
            "tool_name": "read_file",
            "arguments": {"path": "marker.txt"},
            "result_content": tool_result_content,
        },
    )

    _input2, run2, _turn2 = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content="now audit that decision",
    )

    _runtime_task, _history, conversation = await _load_history_via_live_entry(
        owner_sessionmaker, monkeypatch, run_id=run2
    )

    tool_call_entries = [entry for entry in conversation if isinstance(entry.get("tool_calls"), list)]
    assert len(tool_call_entries) == 1, f"expected one assistant tool_call replay entry: {conversation}"
    replay = tool_call_entries[0]["tool_calls"][0]
    assert replay["id"] == tool_use_id
    assert replay["function"]["name"] == "read_file"
    assert "marker.txt" in replay["function"]["arguments"]

    tool_result_entries = [entry for entry in conversation if entry.get("role") == "tool"]
    assert len(tool_result_entries) == 1, f"expected one tool result replay entry: {conversation}"
    assert tool_result_entries[0]["tool_call_id"] == tool_use_id
    assert tool_result_entries[0]["content"] == tool_result_content

    tool_call_index = conversation.index(tool_call_entries[0])
    tool_result_index = conversation.index(tool_result_entries[0])
    assert tool_call_index < tool_result_index
    pairs = _role_content_pairs(conversation)
    assert ("user", first_prompt) in pairs
    assert ("assistant", final_answer) in pairs


async def test_dangling_prior_tool_call_never_replays_without_its_result(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    first_prompt = "crashed mid tool"
    dangling_tool_use_id = f"dangling-{uuid.uuid4().hex}"

    _input1, run1, turn1 = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content=first_prompt,
    )
    await _bind_round_one(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run1,
        turn_id=turn1,
    )
    async with owner_sessionmaker() as db:
        request_id = await _prepare_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run1,
            turn_id=turn1,
            round_index=1,
        )
        # tool_call.started without any tool_result: an interrupted prior run
        await _settle_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run1,
            provider_request_id=request_id,
            provider_tool_use_id=dangling_tool_use_id,
            tool_name="read_file",
            arguments={"path": "missing.txt"},
            result_content=None,
        )
        await _seal_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run1,
            turn_id=turn1,
            round_index=1,
            provider_request_id=request_id,
            content="",
            tool_calls=[
                {
                    "id": dangling_tool_use_id,
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"missing.txt"}',
                    },
                }
            ],
        )
        await db.commit()
    await _set_run_status(owner_sessionmaker, run_id=run1, status="failed")

    _input2, run2, _turn2 = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content="retry the audit",
    )

    runtime_task, _history, conversation = await _load_history_via_live_entry(
        owner_sessionmaker, monkeypatch, run_id=run2
    )

    tool_call_entries = [entry for entry in conversation if isinstance(entry.get("tool_calls"), list)]
    tool_result_entries = [entry for entry in conversation if entry.get("role") == "tool"]
    assert tool_call_entries == [] and tool_result_entries == [], (
        f"a tool_call without its settled result must not replay: {conversation}"
    )
    pairs = _role_content_pairs(conversation)
    assert ("user", first_prompt) in pairs
    receipt = runtime_task.metadata_json["session_semantic_history"]
    assert receipt["status"] == "degraded"
    assert receipt["held_items"][0]["kind"] == "unsettled_tool_round"
    assert receipt["held_items"][0]["missing_provider_tool_use_ids"] == [dangling_tool_use_id]


async def test_current_run_semantics_stay_out_of_history_and_resume_owns_them(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """Permission-resume contract: the current run's own committed rounds are
    reconstructed by the session-permission resume path, not by history."""

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    first_prompt = "first turn prompt"
    first_answer = "first answer"
    second_prompt = "second turn awaiting permission"

    await _run_v2_turn(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        prompt=first_prompt,
        answer=first_answer,
    )

    _input2, run2, turn2 = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content=second_prompt,
    )
    await _bind_round_one(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run2,
        turn_id=turn2,
    )
    async with owner_sessionmaker() as db:
        # the suspended run already committed one partial round of its own
        request_id = await _prepare_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run2,
            turn_id=turn2,
            round_index=1,
        )
        await _seal_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run2,
            turn_id=turn2,
            round_index=1,
            provider_request_id=request_id,
            content="partial answer before permission pause",
        )
        await db.commit()
    await _set_run_status(owner_sessionmaker, run_id=run2, status="suspended")

    _runtime_task, _history, conversation = await _load_history_via_live_entry(
        owner_sessionmaker, monkeypatch, run_id=run2
    )

    pairs = _role_content_pairs(conversation)
    assert ("user", first_prompt) in pairs and ("assistant", first_answer) in pairs
    contents = [content for _role, content in pairs if isinstance(content, str)]
    assert all("partial answer before permission pause" not in content for content in contents), (
        f"current-run assistant semantics leaked into history (resume path owns them): {pairs}"
    )
    user_contents = [content for role, content in pairs if role == "user"]
    assert all(content != second_prompt for content in user_contents), (
        f"current-run input leaked into history (bind_round_inputs owns it): {pairs}"
    )


async def test_materialized_legacy_turns_are_never_duplicated(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """Legacy turns that DID materialize ChatMessage rows must appear exactly
    once, interleaved correctly with later V2-native turns."""

    from app.models.audit import ChatMessage
    from app.services.chat_transcript import append_session_event

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    legacy_prompt = "legacy materialized prompt"
    legacy_answer = "legacy materialized answer"
    v2_prompt = "v2 native prompt"
    v2_answer = "v2 native answer"

    async with owner_sessionmaker() as db:
        await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            actor_type="user",
            event_type="user_message",
            role="user",
            user_id=user_id,
            content=legacy_prompt,
            materialize_chat_message=True,
            source="web",
        )
        await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            actor_type="assistant",
            event_type="assistant_message",
            role="assistant",
            user_id=user_id,
            content=legacy_answer,
            materialize_chat_message=True,
            source="web_chat_runtime",
        )
        await db.commit()
    async with owner_sessionmaker() as db:
        legacy_rows = list(
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.agent_id == agent_id,
                        ChatMessage.conversation_id == str(session_id),
                    )
                )
            ).scalars()
        )
    assert len(legacy_rows) == 2

    await _run_v2_turn(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        prompt=v2_prompt,
        answer=v2_answer,
    )

    _input2, run2, _turn2 = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content="continue the audit",
    )

    _runtime_task, _history, conversation = await _load_history_via_live_entry(
        owner_sessionmaker, monkeypatch, run_id=run2
    )

    pairs = _role_content_pairs(conversation)
    assert pairs.count(("user", legacy_prompt)) == 1, f"legacy user turn duplicated: {pairs}"
    assert pairs.count(("assistant", legacy_answer)) == 1, f"legacy assistant turn duplicated: {pairs}"
    assert pairs.count(("user", v2_prompt)) == 1, f"v2 user turn duplicated: {pairs}"
    assert pairs.count(("assistant", v2_answer)) == 1, f"v2 assistant turn duplicated: {pairs}"
    order = [
        pairs.index(("user", legacy_prompt)),
        pairs.index(("assistant", legacy_answer)),
        pairs.index(("user", v2_prompt)),
        pairs.index(("assistant", v2_answer)),
    ]
    assert order == sorted(order), f"legacy and v2 turns interleaved out of order: {pairs}"


async def test_committed_model_seal_replays_provider_bytes_without_semantic_rewrite(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    input_id, run_id, turn_id = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content="preserve the provider response exactly",
    )
    assert input_id
    await _bind_round_one(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
    )
    signature = "sig:provider-owned:001"
    reasoning = "private provider reasoning bytes"
    async with owner_sessionmaker() as db:
        request_id = await _prepare_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
        )
        seal = await _seal_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=request_id,
            content="exact assistant bytes",
            response_overrides={
                "reasoning_content": reasoning,
                "reasoning_signature": signature,
            },
        )
        await _terminate_run(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            seal=seal,
        )

    _current_input, current_run, _current_turn = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content="next turn",
    )
    _task, _history, conversation = await _load_history_via_live_entry(
        owner_sessionmaker,
        monkeypatch,
        run_id=current_run,
    )
    assistant = next(entry for entry in conversation if entry.get("content") == "exact assistant bytes")
    assert assistant["reasoning_content"] == reasoning
    assert assistant["reasoning_signature"] == signature


async def test_history_loader_has_complete_coverage_before_model_led_compaction(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """The history seam must not pre-trim evidence to a model-derived row cap."""

    import app.services.memory_service as memory_service
    from app.services.chat_transcript import append_session_event

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    marker_count = 25
    monkeypatch.setattr(memory_service, "compute_history_limit", lambda *_args, **_kwargs: 20)
    async with owner_sessionmaker() as db:
        for index in range(marker_count):
            await append_session_event(
                db=db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                actor_type="user",
                event_type="user_message",
                role="user",
                user_id=user_id,
                content=f"history-marker-{index:04d}",
                materialize_chat_message=True,
                source="history-coverage-test",
            )
        await db.commit()

    _current_input, current_run, _current_turn = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content="current input is injected separately",
    )
    task, _history, conversation = await _load_history_via_live_entry(
        owner_sessionmaker,
        monkeypatch,
        run_id=current_run,
    )
    user_contents = [entry.get("content") for entry in conversation if entry.get("role") == "user"]
    assert len(user_contents) == marker_count
    assert user_contents[0] == "history-marker-0000"
    assert user_contents[-1] == "history-marker-0024"
    receipt = task.metadata_json["session_semantic_history"]
    assert receipt["status"] == "complete"
    assert receipt["coverage"]["anchored_legacy_messages"] == marker_count
    assert receipt["mechanical_message_limit_applied"] is False


async def test_unanchored_legacy_and_system_debug_rows_never_enter_provider_history(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.audit import ChatMessage
    from app.services.chat_transcript import append_session_event

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                role="assistant",
                content="UNANCHORED-LEGACY-MUST-NOT-LEAK",
                conversation_id=str(session_id),
            )
        )
        await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            actor_type="system",
            event_type="debug",
            role="system",
            user_id=user_id,
            content="SYSTEM-DEBUG-MUST-NOT-LEAK",
            materialize_chat_message=True,
            source="runtime_debug",
        )
        await db.commit()

    _current_input, current_run, _current_turn = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content="current turn",
    )
    _task, _history, conversation = await _load_history_via_live_entry(
        owner_sessionmaker,
        monkeypatch,
        run_id=current_run,
    )
    serialized = repr(conversation)
    assert "UNANCHORED-LEGACY-MUST-NOT-LEAK" not in serialized
    assert "SYSTEM-DEBUG-MUST-NOT-LEAK" not in serialized
    assert all(entry.get("role") != "system" for entry in conversation)


async def test_pure_v2_rewind_projection_keeps_prefix_and_drops_rewound_turn(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    await _run_v2_turn(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        prompt="rewind prefix prompt",
        answer="rewind prefix answer",
    )
    await _run_v2_turn(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        prompt="rewound prompt",
        answer="rewound answer",
    )
    async with owner_sessionmaker() as db:
        checkpoints = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.schema_version == 2,
                        ChatTranscriptEvent.item_kind == "human_input",
                        ChatTranscriptEvent.lifecycle == "accepted",
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert len(checkpoints) == 2
        session = await db.get(ChatSession, session_id)
        assert session is not None
        session.transcript_metadata_json = {
            **dict(session.transcript_metadata_json or {}),
            "active_projection": {
                "projection_reason": "rewind",
                "checkpoint_event_id": str(checkpoints[1].id),
                "applied_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        await db.commit()

    _current_input, current_run, _current_turn = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content="post-rewind current input",
    )
    _task, _history, conversation = await _load_history_via_live_entry(
        owner_sessionmaker,
        monkeypatch,
        run_id=current_run,
    )
    pairs = _role_content_pairs(conversation)
    assert ("user", "rewind prefix prompt") in pairs
    assert ("assistant", "rewind prefix answer") in pairs
    assert ("user", "rewound prompt") not in pairs
    assert ("assistant", "rewound answer") not in pairs


async def test_real_branch_consumes_copied_canonical_v2_prefix(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.user import User
    from app.services.conversation_branch_service import create_conversation_branch

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    await _run_v2_turn(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        prompt="branch prefix prompt",
        answer="branch prefix answer",
    )
    await _run_v2_turn(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        prompt="branch anchor prompt",
        answer="source-only answer",
    )
    async with owner_sessionmaker() as db:
        checkpoints = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.schema_version == 2,
                        ChatTranscriptEvent.item_kind == "human_input",
                        ChatTranscriptEvent.lifecycle == "accepted",
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert len(checkpoints) == 2
        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        source_session = await db.get(ChatSession, session_id)
        assert agent is not None and user is not None and source_session is not None
        branch = await create_conversation_branch(
            db=db,
            agent=agent,
            user=user,
            source_session=source_session,
            mode="branch",
            anchor_event_id=checkpoints[1].id,
        )
        branch_session_id = branch.session.id
        await db.commit()

    _current_input, branch_run, _current_turn = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=branch_session_id,
        content="branch current input",
    )
    task, _history, conversation = await _load_history_via_live_entry(
        owner_sessionmaker,
        monkeypatch,
        run_id=branch_run,
    )
    pairs = _role_content_pairs(conversation)
    assert ("user", "branch prefix prompt") in pairs
    assert ("assistant", "branch prefix answer") in pairs
    assert ("user", "branch anchor prompt") not in pairs
    assert ("assistant", "source-only answer") not in pairs
    receipt = task.metadata_json["session_semantic_history"]
    assert receipt["branch_prefix"]["status"] == "resolved"
    assert receipt["branch_prefix"]["source_session_id"] == str(session_id)


async def test_edit_branch_live_api_binds_full_unicode_retry_input_to_round_one(
    owner_sessionmaker,
) -> None:
    """SESSION-RETRY-INPUT-001: the live branch API must not bypass V2 input.

    Production reproduced a completed edit retry whose canonical checkpoint
    held the full prompt while ``result_commit.prepared.bound_input_ids`` was
    empty. Drive the real API entry and PostgreSQL aggregates, then prove the
    exact long Unicode input is the sole durable round-one user message.
    """

    from app.api import chat_sessions as chat_sessions_api
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionTurnInput
    from app.models.user import User
    from app.services.session_model_round import bind_round_inputs

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content="source turn before a retryable provider failure",
    )
    retry_prompt = (
        "WEEKEND-RC retry ①：请逐字保留当前输入，不要把 queue ordinal 1 当作消息。\n"
        "Marker: RETRY-完整输入-雪松-734\n" + "活动议程与风险恢复证据；" * 180
    )

    async with owner_sessionmaker() as db:
        anchor = await db.scalar(
            select(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.schema_version == 2,
                ChatTranscriptEvent.item_kind == "human_input",
                ChatTranscriptEvent.lifecycle == "accepted",
            )
            .order_by(ChatTranscriptEvent.sequence.desc())
        )
        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        source_session = await db.get(ChatSession, session_id)
        assert anchor is not None and agent is not None and user is not None and source_session is not None

        response = await chat_sessions_api.branch_session(
            agent_id=agent_id,
            session_id=session_id,
            body=chat_sessions_api.BranchSessionIn(
                mode="edit",
                anchor_event_id=anchor.id,
                content=retry_prompt,
                display_content=retry_prompt,
                start_run=True,
                permission_mode="default",
            ),
            current_user=user,
            db=db,
        )
        branch_session_id = uuid.UUID(response.session.id)
        run_payload = dict(response.run or {})
        run_id = uuid.UUID(str(run_payload["run_id"]))
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        turn_id = str((task.metadata_json or {})["turn_id"])

        row = await db.scalar(
            select(SessionTurnInput).where(
                SessionTurnInput.tenant_id == tenant_id,
                SessionTurnInput.session_id == branch_session_id,
                SessionTurnInput.target_run_id == run_id,
            )
        )
        assert row is not None
        assert row.content_parts_json == [{"type": "text", "text": retry_prompt, "display_content": retry_prompt}]

        bound_messages = await bind_round_inputs(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=branch_session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
        )
        await db.commit()

        assert len(bound_messages) == 1
        assert bound_messages[0]["role"] == "user"
        assert bound_messages[0]["content"] == retry_prompt
        model_result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == run_id))
        assert model_result is not None
        assert model_result.bound_input_ids_json == [str(row.id)]


async def test_valid_empty_history_is_typed_and_not_unavailable(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    _tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    _current_input, current_run, _current_turn = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content="first current input",
    )
    task, history, conversation = await _load_history_via_live_entry(
        owner_sessionmaker,
        monkeypatch,
        run_id=current_run,
    )
    assert history == []
    assert conversation == []
    assert task.metadata_json["session_semantic_history"]["status"] == "empty"


async def test_history_unavailable_fails_closed_before_any_provider_call(monkeypatch) -> None:
    import app.services.web_chat_runtime as runtime
    from app.services.session_semantic_history import SessionSemanticHistoryUnavailable

    run_id, tenant_id, agent_id, session_id = (uuid.uuid4() for _ in range(4))
    terminal_calls = []
    provider_calls = []

    async def unavailable(_run_id):
        raise SessionSemanticHistoryUnavailable(
            code="canonical_session_history_unavailable",
            message="canonical transcript read failed",
            run_id=run_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            retryable=True,
            evidence_refs=("chat_transcript_events",),
        )

    async def forbidden_provider(_request):
        provider_calls.append(True)
        raise AssertionError("provider must not run without canonical history")

    async def finalize_without_assistant(**kwargs):
        terminal_calls.append(("finalize", kwargs))
        return True

    async def emit_terminal_hook(**kwargs):
        terminal_calls.append(("hook", kwargs))

    async def update_runtime_task(*_args, **kwargs):
        terminal_calls.append(("update", kwargs))

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", unavailable)
    monkeypatch.setattr(runtime, "invoke_agent", forbidden_provider)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_without_assistant", finalize_without_assistant)
    monkeypatch.setattr(runtime, "_emit_terminal_turn_hook", emit_terminal_hook)
    monkeypatch.setattr(runtime, "_update_runtime_task", update_runtime_task)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop)

    await runtime.execute_web_chat_run(run_id)

    assert provider_calls == []
    finalize = next(payload for kind, payload in terminal_calls if kind == "finalize")
    assert finalize["agent_id"] == agent_id
    assert finalize["session_id"] == str(session_id)
    assert finalize["status"] == "failed"
    assert finalize["metadata_json"] == {
        "delivery_state": "not_started",
        "error": "canonical transcript read failed",
        "error_code": "canonical_session_history_unavailable",
        "retryable": True,
        "session_semantic_history": {
            "evidence_refs": ["chat_transcript_events"],
            "error_code": "canonical_session_history_unavailable",
            "retryable": True,
            "status": "unavailable",
        },
        "terminal_reason": "persistence_error",
    }
