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

    # These tests drive the model worker seam directly, so bind the same
    # pending -> running claim the production worker establishes first.
    await _set_run_status(owner_sessionmaker, run_id=run_id, status="running")
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
    messages=None,
    wire_request=None,
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
        messages=messages if messages is not None else [{"role": "user", "content": "marker"}],
        tools=None,
        provider="openai",
        model="gpt-test",
        wire_request=wire_request if wire_request is not None else {"messages": [], "tools": []},
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
    model_visible_content=None,
    outcome="success",
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
    if outcome in {"success", "failed"}:
        await mark_tool_effect_started(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=invocation.id,
        )
    from app.services.session_tool_runtime import _sha256

    await complete_tool_invocation(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        invocation_id=invocation.id,
        provider_result_content=result_content,
        model_visible_content=model_visible_content,
        execution_evidence={
            "schema": "hive.tool_execution_evidence.v1",
            "status": outcome if outcome in {"cancelled", "aborted"} else "settled",
            "pre_effect_fence_ref": "test-pre-effect-fence",
            "retryable": False,
            "tool_decision": {
                "schema": "hive.tool_decision.v1",
                "decision_id": f"decision-{provider_tool_use_id}",
                "outcome": {"denied": "deny", "unavailable": "unavailable", "approval_required": "require_approval"}.get(outcome, "allow"),
                "input_hash": invocation.args_hash,
                "policy_snapshot_hash": "a" * 64,
                "capability_snapshot_hash": "b" * 64,
            },
            "execution_frame": {
                "status": "failed" if outcome == "failed" else "completed",
                "output_hash": _sha256(str(result_content)),
            },
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
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox

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
    boundaries = list(
        (
            await db.execute(
                select(RuntimeTerminalBoundaryOutbox).where(
                    RuntimeTerminalBoundaryOutbox.tenant_id == tenant_id,
                    RuntimeTerminalBoundaryOutbox.session_id == str(session_id),
                    RuntimeTerminalBoundaryOutbox.status == "pending",
                )
            )
        ).scalars()
    )
    for boundary in boundaries:
        boundary.status = "delivered"
        boundary.delivered_at = datetime.now(timezone.utc)
        boundary.delivery_receipt_json = {"test_consumer": "session_semantic_history"}
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
                model_visible_content=tool.get("model_visible_content"),
                outcome=tool.get("outcome", "success"),
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
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services.runtime_terminal_settlement import (
        TERMINAL_SETTLEMENT_STATUSES,
        settle_and_enqueue_runtime_task_terminal,
    )

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        task.status = status
        if status in TERMINAL_SETTLEMENT_STATUSES:
            task.completed_at = datetime.now(timezone.utc)
            await settle_and_enqueue_runtime_task_terminal(
                db,
                task,
                terminal_source="session_semantic_history.test_fixture",
                root_reason_code="test_terminal_transition",
            )
            boundaries = list(
                (
                    await db.execute(
                        select(RuntimeTerminalBoundaryOutbox).where(
                            RuntimeTerminalBoundaryOutbox.runtime_task_id == task.id,
                            RuntimeTerminalBoundaryOutbox.status == "pending",
                        )
                    )
                ).scalars()
            )
            for boundary in boundaries:
                boundary.status = "delivered"
                boundary.delivered_at = datetime.now(timezone.utc)
                boundary.delivery_receipt_json = {"test_consumer": "session_semantic_history"}
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


async def test_duplicate_prepare_preserves_committed_provider_seal(owner_sessionmaker) -> None:
    from copy import deepcopy

    from app.models.session_v2 import SessionModelResult
    from app.services.session_model_round import ModelRoundNeedsReconciliation

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    run_id, turn_id = await _run_v2_turn(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        prompt="immutable request",
        answer="immutable provider result",
    )
    async with owner_sessionmaker() as db:
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == run_id))
        before = (result.state, result.version, result.round_committed_event_id, deepcopy(result.seal_json))
        with pytest.raises(ModelRoundNeedsReconciliation):
            await _prepare_round(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                round_index=1,
            )
        # The live error path can commit its error receipt; it must not corrupt
        # a previously committed aggregate in that same transaction.
        await db.commit()
        await db.refresh(result)
        assert (result.state, result.version, result.round_committed_event_id, result.seal_json) == before


@pytest.mark.parametrize("damage", ["ambiguous_prepare", "missing_commit", "wrong_commit", "other_reconciliation"])
async def test_history_recovers_only_exact_committed_prepare_drift(
    owner_sessionmaker,
    monkeypatch,
    damage,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionModelResult
    from app.services.session_semantic_history import SessionSemanticHistoryUnavailable

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    run_id, _turn_id = await _run_v2_turn(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        prompt="prior request",
        answer="preserved committed bytes",
    )
    async with owner_sessionmaker() as db:
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == run_id))
        assert result.round_committed_event_id is not None
        result.state = "needs_reconciliation"
        result.reconciliation_owner = "session_model_round:ambiguous_prepare"
        if damage == "missing_commit":
            result.round_committed_event_id = None
        elif damage == "wrong_commit":
            result.round_committed_event_id = await db.scalar(
                select(ChatTranscriptEvent.id)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.id != result.round_committed_event_id,
                )
                .limit(1)
            )
        elif damage == "other_reconciliation":
            result.reconciliation_owner = "session_model_round:ambiguous_failure"
        if damage == "wrong_commit":
            from sqlalchemy.exc import IntegrityError

            # The existing database authority trigger rejects this corruption
            # even before a history reader can encounter it.
            with pytest.raises(IntegrityError, match="session_v2_authority_binding_mismatch"):
                await db.commit()
            await db.rollback()
            return
        await db.commit()
    _input_id, next_run, _turn_id = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content="continue",
    )
    if damage != "ambiguous_prepare":
        with pytest.raises(SessionSemanticHistoryUnavailable) as error:
            await _load_history_via_live_entry(owner_sessionmaker, monkeypatch, run_id=next_run)
        assert error.value.code == "committed_model_seal_unavailable"
    else:
        _task, _history, conversation = await _load_history_via_live_entry(
            owner_sessionmaker,
            monkeypatch,
            run_id=next_run,
        )
        assert ("assistant", "preserved committed bytes") in _role_content_pairs(conversation)


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


@pytest.mark.parametrize("outcome", ["success", "failed", "denied", "unavailable", "cancelled", "aborted"])
async def test_prior_turn_tool_semantics_replay_in_provider_history(
    owner_sessionmaker,
    monkeypatch,
    outcome,
) -> None:
    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    first_prompt = "read the marker file and decide"
    tool_use_id = f"tool-use-{uuid.uuid4().hex}"
    tool_result_content = f"{outcome}: WEEKEND-RC"
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
            "outcome": outcome,
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

    # Compact uses the same canonical reader without a current run.
    from app.services.session_semantic_history import load_session_semantic_history

    async with owner_sessionmaker() as db:
        history = await load_session_semantic_history(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id)
    assert history.receipt["held_items"] == []
    assert any(message.role == "tool" and message.content == tool_result_content for message in history.messages)


@pytest.mark.parametrize("empty_projection", [False, True])
async def test_enveloped_tool_result_persists_raw_receipt_and_replays_projection(
    owner_sessionmaker,
    monkeypatch,
    empty_projection,
) -> None:
    """B4 HR handoff regression: same-envelope tool results must persist the
    complete raw receipt as durable transcript evidence while session replay
    feeds the bounded provider projection back into model context."""

    import json as _json

    from app.models.chat_transcript_event import ChatTranscriptEvent

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    tool_use_id = f"handoff-{uuid.uuid4().hex}"
    hr_agent_id = str(uuid.uuid4())
    hr_session_id = str(uuid.uuid4())
    raw_receipt = _json.dumps(
        {
            "ok": True,
            "status": "hr_handoff_started",
            "hr_agent_id": hr_agent_id,
            "hr_session_id": hr_session_id,
            "source_agent_name": "Source Agent",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    model_projection = _json.dumps(
        {"ok": True, "status": "hr_handoff_ready", "message": "direct the user to the handoff card"},
        ensure_ascii=False,
        sort_keys=True,
    )
    if empty_projection:
        model_projection = ""

    await _run_v2_turn(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        prompt="hire a new digital employee",
        answer="handed off to HR Agent",
        tool={
            "provider_tool_use_id": tool_use_id,
            "tool_name": "start_hr_agent_handoff",
            "arguments": {"creation_brief": "brief"},
            "result_content": raw_receipt,
            "model_visible_content": model_projection,
        },
    )

    async with owner_sessionmaker() as db:
        result_events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.item_kind == "tool_result",
                        ChatTranscriptEvent.lifecycle == "completed",
                    )
                )
            ).scalars()
        )
    assert len(result_events) == 1, "expected exactly one settled tool_result event"
    event = result_events[0]
    assert event.content == raw_receipt, "durable transcript must keep the complete raw receipt"
    persisted = (event.metadata_json or {}).get("v2_payload") or {}
    assert persisted.get("model_visible_content") == model_projection

    _input2, run2, _turn2 = await _submit_turn(
        owner_sessionmaker,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        content="continue with the handoff",
    )
    _runtime_task, _history, conversation = await _load_history_via_live_entry(
        owner_sessionmaker, monkeypatch, run_id=run2
    )
    tool_result_entries = [entry for entry in conversation if entry.get("role") == "tool"]
    assert len(tool_result_entries) == 1
    replay_content = tool_result_entries[0]["content"]
    assert replay_content == model_projection, "provider replay must use the declared projection"
    assert hr_agent_id not in replay_content and hr_session_id not in replay_content, (
        "internal ids must not leak into model context"
    )
    assert hr_agent_id in (event.content or ""), "raw evidence retained for UI/audit"


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


async def _stop_mid_tool_call(
    db,
    *,
    tenant_id,
    agent_id,
    session_id,
    run_id,
    turn_id,
    provider_request_id,
    tool_use_id,
    spec,
):
    """Drive one issued call to the durable shape the stop will catch it in.

    ``kind`` selects the shape: ``prepared`` (prepare_tool_invocation only),
    ``fence`` (through the real mark_tool_effect_started effect fence, no
    result), ``settled`` (complete_tool_invocation success),
    ``needs_reconciliation`` (quarantined after prepare), and
    ``approval_ticket_broken`` (an approval_required result whose enterprise
    ticket stops matching after the write).
    """

    import json as _json

    tool_name = str(spec.get("tool") or "web_search")
    arguments = dict(spec.get("arguments") or {"query": f"news-{tool_use_id.hex[:6]}"})
    kind = spec.get("kind", "prepared")
    if kind == "approval_ticket_broken":
        from pathlib import Path

        from app.models.audit import ApprovalRequest
        from app.services.approval_ticket import (
            build_approval_execution_envelope,
            hash_approval_execution_envelope,
            hash_tool_input,
        )
        from app.services.session_tool_runtime import complete_tool_invocation, prepare_tool_invocation
        from app.tools.runtime import ToolExecutionContext

        invocation = await prepare_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            provider_request_id=provider_request_id,
            provider_tool_use_id=tool_use_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        approval_id = uuid.uuid4()
        decision_id = f"decision-{tool_use_id}"
        envelope = build_approval_execution_envelope(
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=spec["user_id"],
                tenant_id=str(tenant_id),
                workspace=Path("/synthetic/workspace"),
                session_id=str(session_id),
                turn_id=turn_id,
                runtime_task_id=str(run_id),
            ),
            tool_call_id=tool_use_id,
            emit_runtime_hooks=False,
        )
        ticket = ApprovalRequest(
            id=approval_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            action_type=tool_name,
            requested_by=spec["user_id"],
            decision_id=decision_id,
            tool_name=tool_name,
            normalized_arguments=dict(arguments),
            input_hash=hash_tool_input(tool_name, arguments),
            execution_envelope=envelope,
            execution_envelope_hash=hash_approval_execution_envelope(envelope),
        )
        db.add(ticket)
        await db.flush()
        await complete_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=invocation.id,
            provider_result_content=_json.dumps(
                {"status": "approval_required", "approval_id": str(approval_id), "tool_name": tool_name}
            ),
            execution_evidence={
                "schema": "hive.tool_execution_evidence.v1",
                "status": "settled",
                "pre_effect_fence_ref": "test-pre-effect-fence",
                "retryable": False,
                "tool_decision": {
                    "schema": "hive.tool_decision.v1",
                    "decision_id": decision_id,
                    "outcome": "require_approval",
                    "approval_id": str(approval_id),
                    "input_hash": invocation.args_hash,
                    "policy_snapshot_hash": "a" * 64,
                    "capability_snapshot_hash": "b" * 64,
                },
                "execution_frame": {"status": "completed", "output_hash": "c" * 64},
            },
        )
        # The write-side check passed with the original decision id; rotating it
        # afterwards leaves the read-side ticket check failing.
        ticket.decision_id = f"{decision_id}-rotated"
        await db.flush()
        return invocation
    result_content = spec.get("result_content")
    invocation = await _settle_tool_invocation(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        provider_request_id=provider_request_id,
        provider_tool_use_id=tool_use_id,
        tool_name=tool_name,
        arguments=arguments,
        result_content=result_content if kind == "settled" else None,
        outcome=spec.get("outcome", "success"),
    )
    if kind == "fence":
        from app.services.session_tool_runtime import mark_tool_effect_started

        await mark_tool_effect_started(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=invocation.id,
        )
    elif kind == "needs_reconciliation":
        from app.services.session_tool_runtime import mark_tool_invocation_needs_reconciliation

        await mark_tool_invocation_needs_reconciliation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=invocation.id,
            reason_code="test_effect_receipt_lost",
            recovery_owner="session_tool_runtime:effect_receipt_pending",
        )
    return invocation


async def _member_stopped_mid_tool_session(
    owner_sessionmaker,
    monkeypatch,
    *,
    prompt,
    followup,
    calls,
    member_stop=True,
):
    """A run the member stopped while issued tool calls never settled.

    Drives the real ingress, model-round sealing, tool-invocation lifecycle,
    member-stop control input, and canonical terminal settlement, then loads
    the successor turn's provider conversation through the live entry. With
    ``member_stop=False`` the run is killed without a stop control instead.
    """

    import json as _json

    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services import web_chat_runtime as runtime
    from app.services.session_live_input import submit_live_cancel_input, submit_live_human_input

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    plan_text = "开始检索，先并行跑几组关键词"
    _input1, run1, turn1 = await _submit_turn(
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
        run_id=run1,
        turn_id=turn1,
    )
    tool_use_ids, invocations, sealed_calls = [], {}, []
    async with owner_sessionmaker() as db:
        request_id = await _prepare_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run1,
            turn_id=turn1,
            round_index=1,
            # Bind the round the way production does: the request that sealed
            # this round carried the turn's own input as a resolvable source.
            messages=[{"role": "user", "content": prompt, "source_ref": f"session-input:{_input1}"}],
            wire_request={"messages": [{"role": "user", "content": prompt}], "tools": []},
        )
        for index, spec in enumerate(calls):
            # An explicit "id" lets a test reproduce a seal whose provider id
            # carries bytes (e.g. a trailing space) that prepare_tool_invocation
            # strips before storing the invocation.
            tool_use_id = str(spec.get("id") or f"search-{uuid.uuid4().hex}")
            arguments = dict(spec.get("arguments") or {"query": f"news-{index}"})
            invocation = await _stop_mid_tool_call(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run1,
                turn_id=turn1,
                provider_request_id=request_id,
                tool_use_id=tool_use_id,
                spec={"user_id": user_id, **spec, "arguments": arguments},
            )
            tool_use_ids.append(tool_use_id)
            invocations[tool_use_id] = invocation
            sealed_calls.append(
                {
                    "id": tool_use_id,
                    "type": "function",
                    "function": {
                        "name": str(spec.get("tool") or "web_search"),
                        "arguments": _json.dumps(arguments, ensure_ascii=False),
                    },
                }
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
            content=plan_text,
            tool_calls=sealed_calls,
        )
        await db.commit()

    if member_stop:
        async def signal(**_kwargs):
            return None

        monkeypatch.setattr(runtime, "signal_web_chat_cancel", signal)
        async with owner_sessionmaker() as db:
            receipt = await submit_live_cancel_input(
                db=db,
                agent=await db.get(Agent, agent_id),
                user=await db.get(User, user_id),
                session=await db.get(ChatSession, session_id),
                run_id=run1,
                source="web",
            )
            await db.commit()
        assert receipt["status"] == "applying", receipt
    await _set_run_status(owner_sessionmaker, run_id=run1, status="killed")

    input_id, run2, turn2 = uuid.uuid4(), None, None
    async with owner_sessionmaker() as db:
        followup_receipt = await submit_live_human_input(
            db=db,
            agent=await db.get(Agent, agent_id),
            user=await db.get(User, user_id),
            session=await db.get(ChatSession, session_id),
            content=followup,
            source="web",
            input_id=input_id,
            idempotency_key=f"live:{input_id}",
        )
        await db.commit()
    assert followup_receipt["admission_state"] == "admitted", followup_receipt
    run2 = uuid.UUID(str((followup_receipt.get("run") or {})["run_id"]))
    turn2 = str((followup_receipt.get("run") or {})["turn_id"])
    _runtime_task, history_messages, conversation = await _load_history_via_live_entry(
        owner_sessionmaker, monkeypatch, run_id=run2
    )
    return {
        "run1": run1,
        "run2": run2,
        "turn2": turn2,
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "tool_use_ids": tool_use_ids,
        "invocations": invocations,
        "plan_text": plan_text,
        "prompt": prompt,
        "followup": followup,
        "admission": followup_receipt["admission_state"],
        "history_messages": history_messages,
        "conversation": conversation,
        "receipt": _runtime_task.metadata_json["session_semantic_history"],
    }


async def _next_turn_model_input(owner_sessionmaker, *, stopped):
    """Run the real next-round source binding and withholding pass.

    Round 1 stopped at the assembled conversation; this converts it exactly
    like the model-round prepare path (``build_model_input_bindings`` +
    ``_validate_or_withhold_model_sources``) and returns the bindings plus the
    post-withholding wire messages the provider adapter would receive.
    """

    from app.kernel.engine import _dicts_to_llm_messages, _llm_messages_to_dicts
    from app.services.session_model_inputs import build_model_input_bindings
    from app.services.session_model_round import _validate_or_withhold_model_sources

    async with owner_sessionmaker() as db:
        messages = _dicts_to_llm_messages(stopped["conversation"])
        wire = {"messages": _llm_messages_to_dicts(messages), "tools": []}
        bindings = await build_model_input_bindings(
            db,
            tenant_id=stopped["tenant_id"],
            agent_id=stopped["agent_id"],
            session_id=stopped["session_id"],
            messages=messages,
            wire_messages=wire["messages"],
        )
        await _validate_or_withhold_model_sources(
            db,
            tenant_id=stopped["tenant_id"],
            agent_id=stopped["agent_id"],
            session_id=stopped["session_id"],
            run_id=stopped["run2"],
            input_bindings=bindings,
            messages=messages,
            wire_request=wire,
        )
        await db.commit()
        return bindings, wire["messages"]


async def test_member_stop_replays_interrupted_tool_calls_to_next_turn(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """Run13 defect: the next turn's model input must show the two search calls
    the stop cut off as interrupted with no result, not erase the round."""

    from app.services import session_semantic_history as history

    stopped = await _member_stopped_mid_tool_session(
        owner_sessionmaker,
        monkeypatch,
        prompt="搜索新闻并写一篇综述",
        followup="刚才做了什么？",
        calls=[{"kind": "prepared"}, {"kind": "prepared"}],
    )
    conversation = stopped["conversation"]
    search_one, search_two = stopped["tool_use_ids"]

    tool_call_entries = [entry for entry in conversation if isinstance(entry.get("tool_calls"), list)]
    assert len(tool_call_entries) == 1, f"stopped round's assistant calls missing: {conversation}"
    assert [call["id"] for call in tool_call_entries[0]["tool_calls"]] == [search_one, search_two]
    assert tool_call_entries[0]["content"] == stopped["plan_text"], (
        f"stopped round's plan text missing: {conversation}"
    )

    tool_result_entries = [entry for entry in conversation if entry.get("role") == "tool"]
    assert sorted(entry["tool_call_id"] for entry in tool_result_entries) == sorted(
        [search_one, search_two]
    ), f"interrupted calls must replay with one result each: {conversation}"
    assert all(
        entry["content"] == history._INTERRUPTED_TOOL_CALL_NEVER_RAN for entry in tool_result_entries
    ), f"interrupted results must carry the fixed no-result sentence: {tool_result_entries}"
    for entry in tool_result_entries:
        assert conversation.index(tool_call_entries[0]) < conversation.index(entry), (
            f"each interrupted result must follow its assistant call: {conversation}"
        )

    contents = [entry.get("content") for entry in conversation]
    assert contents.index(stopped["prompt"]) < contents.index(stopped["plan_text"])
    assert contents.index(stopped["plan_text"]) < contents.index(history._USER_STOP_MARKER)
    assert stopped["receipt"]["held_items"] == [], stopped["receipt"]


async def test_interrupted_stop_replay_pairs_for_provider_adapters(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """The assembled stop history converts validly for the adapters in use:
    every tool call id has exactly one result, after its call, in both the
    OpenAI and Anthropic wire formats."""

    from app.kernel.engine import _dicts_to_llm_messages

    stopped = await _member_stopped_mid_tool_session(
        owner_sessionmaker,
        monkeypatch,
        prompt="检索行业动态",
        followup="继续",
        calls=[{"kind": "prepared"}, {"kind": "prepared"}],
    )
    api_messages = _dicts_to_llm_messages(stopped["conversation"])
    search_one, search_two = stopped["tool_use_ids"]

    openai_entries = [message.to_openai_format() for message in api_messages]
    issued = [
        call["id"]
        for entry in openai_entries
        for call in (entry.get("tool_calls") or [])
    ]
    answered = [entry.get("tool_call_id") for entry in openai_entries if entry.get("role") == "tool"]
    assert issued == [search_one, search_two]
    assert sorted(answered) == sorted([search_one, search_two])

    anthropic_entries = [
        converted for message in api_messages if (converted := message.to_anthropic_format()) is not None
    ]
    block_order: list[tuple[str, str]] = []
    for entry in anthropic_entries:
        blocks = entry.get("content") if isinstance(entry.get("content"), list) else []
        for block in blocks:
            if block.get("type") == "tool_use":
                block_order.append(("use", block["id"]))
            elif block.get("type") == "tool_result":
                block_order.append(("result", block["tool_use_id"]))
    assert [use_id for kind, use_id in block_order if kind == "use"] == [search_one, search_two]
    assert sorted(use_id for kind, use_id in block_order if kind == "result") == sorted(
        [search_one, search_two]
    )
    for use_id in (search_one, search_two):
        use_at = block_order.index(("use", use_id))
        result_at = block_order.index(("result", use_id))
        assert use_at < result_at, f"tool_result for {use_id} must follow its tool_use: {block_order}"


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

    async def update_runtime_task(*_args, **kwargs):
        terminal_calls.append(("update", kwargs))

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", unavailable)
    monkeypatch.setattr(runtime, "invoke_agent", forbidden_provider)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_without_assistant", finalize_without_assistant)
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


async def test_an_unbound_company_approval_round_remains_held(owner_sessionmaker, monkeypatch):
    import json

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    tool_id = uuid.uuid4().hex
    marker = "UNBOUND_COMPANY_TICKET_PROPOSAL"
    await _run_v2_turn(owner_sessionmaker, tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
        session_id=session_id, prompt="Prepare the authorized notification.", answer="Waiting for the company decision.",
        tool={"provider_tool_use_id": tool_id, "tool_name": "send_web_message",
            "arguments": {"username": "recipient", "message": marker}, "outcome": "approval_required",
            "result_content": json.dumps({"status": "approval_required", "approval_id": str(uuid.uuid4()),
                "tool_name": "send_web_message"})})
    _input, next_run, _turn = await _submit_turn(owner_sessionmaker, user_id=user_id, agent_id=agent_id,
        session_id=session_id, content="Inspect the prior action.")
    task, _history, conversation = await _load_history_via_live_entry(owner_sessionmaker, monkeypatch, run_id=next_run)
    assert marker not in json.dumps(conversation)
    held = task.metadata_json["session_semantic_history"]["held_items"]
    assert any(item["kind"] == "unsettled_tool_round" and item["missing_provider_tool_use_ids"] == [tool_id] for item in held)


async def test_interrupted_notices_survive_real_next_turn_source_binding(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """B1: the synthetic interrupted notice is a resolvable model-input source.

    Round 1 stopped at the assembled conversation; through the real
    build_model_input_bindings + _validate_or_withhold_model_sources pass the
    next turn's wire must keep the plan text, both issued calls with their
    arguments, one fixed notice per call, the stop marker — and must not
    replace them with the un-verifiable-context system note.
    """

    import json

    from app.services import session_semantic_history as history

    stopped = await _member_stopped_mid_tool_session(
        owner_sessionmaker,
        monkeypatch,
        prompt="搜索新闻并写一篇综述",
        followup="刚才做了什么？",
        calls=[{"kind": "prepared"}, {"kind": "prepared"}],
    )
    bindings, wire = await _next_turn_model_input(owner_sessionmaker, stopped=stopped)
    search_one, search_two = stopped["tool_use_ids"]

    entries = {entry.get("source_ref"): entry for entry in bindings["messages"]}
    interrupted_entries = [ref for ref in entries if str(ref).startswith("session-tool-interrupted:")]
    assert sorted(str(ref).rsplit(":", 1)[1] for ref in interrupted_entries) == sorted(
        [search_one, search_two]
    ), bindings["messages"]
    assert all(
        entries[ref]["status"] == "bound" and entries[ref]["dependency_status"] == "resolved"
        for ref in interrupted_entries
    ), [entries[ref] for ref in interrupted_entries]

    contents = [message.get("content") for message in wire]
    assert stopped["plan_text"] in contents, wire
    assistant = next(message for message in wire if message.get("content") == stopped["plan_text"])
    assert [call["id"] for call in assistant.get("tool_calls") or []] == [search_one, search_two]
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"query": "news-0"}, wire
    assert json.loads(assistant["tool_calls"][1]["function"]["arguments"]) == {"query": "news-1"}, wire

    tool_messages = [message for message in wire if message.get("role") == "tool"]
    assert sorted(message["tool_call_id"] for message in tool_messages) == sorted([search_one, search_two])
    assert all(
        message["content"] == history._INTERRUPTED_TOOL_CALL_NEVER_RAN for message in tool_messages
    ), tool_messages
    assert history._USER_STOP_MARKER in contents, wire
    assert "无法核实" not in json.dumps(wire, ensure_ascii=False), wire
    # The synthetic notices are read-model pairing, not settled results.
    assert stopped["receipt"]["coverage"]["settled_tool_results"] == 0, stopped["receipt"]


async def test_started_effect_call_cut_by_stop_reaches_next_turn_model_input(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """B2/run13: a call that crossed the real effect fence and was cut off
    before any result pairs with the unknown-effect sentence, both in the
    assembled conversation and on the post-binding wire."""

    import json

    from app.services import session_semantic_history as history

    stopped = await _member_stopped_mid_tool_session(
        owner_sessionmaker,
        monkeypatch,
        prompt="搜索新闻并写一篇综述",
        followup="刚才做了什么？",
        calls=[{"kind": "fence"}],
    )
    search_id, = stopped["tool_use_ids"]

    async with owner_sessionmaker() as db:
        from app.models.session_v2 import SessionToolInvocation

        invocation = await db.get(SessionToolInvocation, stopped["invocations"][search_id].id)
        assert (invocation.effect_state, invocation.result_event_id) == ("effect_started", None)

    conversation = stopped["conversation"]
    conversation_contents = [entry.get("content") for entry in conversation]
    assert stopped["plan_text"] in conversation_contents, conversation
    tool_result_entries = [entry for entry in conversation if entry.get("role") == "tool"]
    assert [entry["content"] for entry in tool_result_entries] == [
        history._INTERRUPTED_TOOL_CALL_NO_RESULT
    ], conversation

    bindings, wire = await _next_turn_model_input(owner_sessionmaker, stopped=stopped)
    contents = [message.get("content") for message in wire]
    assert stopped["plan_text"] in contents, wire
    tool_messages = [message for message in wire if message.get("role") == "tool"]
    assert [(message["tool_call_id"], message["content"]) for message in tool_messages] == [
        (search_id, history._INTERRUPTED_TOOL_CALL_NO_RESULT)
    ], wire
    assert history._USER_STOP_MARKER in contents, wire
    assert "无法核实" not in json.dumps(wire, ensure_ascii=False), wire
    assert stopped["receipt"]["held_items"] == [], stopped["receipt"]


async def test_padded_seal_tool_call_id_pairs_via_stripped_lookup_and_resolves_with_original(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """Reviewer nit 3 regression: the seal may carry a provider id with a
    trailing space while prepare_tool_invocation stores the stripped form.
    The interrupted pairing must find the invocation through the stripped
    lookup, keep the seal's original bytes as the wire tool_call_id, and the
    resolver must accept the ref built from the original id."""

    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services import session_semantic_history as history
    from app.services.session_model_inputs import _resolve_origin_dependencies

    stopped = await _member_stopped_mid_tool_session(
        owner_sessionmaker,
        monkeypatch,
        prompt="搜索新闻",
        followup="刚才做了什么？",
        calls=[{"kind": "fence", "id": "toolu_x "}],
    )
    sealed_id, = stopped["tool_use_ids"]
    assert sealed_id == "toolu_x "

    async with owner_sessionmaker() as db:
        invocation, = (
            await db.scalars(
                select(SessionToolInvocation).where(SessionToolInvocation.run_id == stopped["run1"])
            )
        ).all()
        assert (invocation.provider_tool_use_id, invocation.effect_state, invocation.result_event_id) == (
            "toolu_x",
            "effect_started",
            None,
        ), invocation

    tool_result_entries = [entry for entry in stopped["conversation"] if entry.get("role") == "tool"]
    assert [(entry.get("tool_call_id"), entry["content"]) for entry in tool_result_entries] == [
        ("toolu_x ", history._INTERRUPTED_TOOL_CALL_NO_RESULT)
    ], stopped["conversation"]

    bindings, wire = await _next_turn_model_input(owner_sessionmaker, stopped=stopped)
    tool_messages = [message for message in wire if message.get("role") == "tool"]
    assert [(message["tool_call_id"], message["content"]) for message in tool_messages] == [
        ("toolu_x ", history._INTERRUPTED_TOOL_CALL_NO_RESULT)
    ], wire
    assistant_message = next(message for message in wire if message.get("role") == "assistant")
    assert [call["id"] for call in assistant_message["tool_calls"]] == ["toolu_x "], wire
    assert stopped["receipt"]["held_items"] == [], stopped["receipt"]

    async with owner_sessionmaker() as db:
        result = await db.scalar(
            select(SessionModelResult).where(SessionModelResult.run_id == stopped["run1"])
        )
        assert result is not None
        assert await _resolve_origin_dependencies(
            db,
            tenant_id=stopped["tenant_id"],
            agent_id=stopped["agent_id"],
            ref=f"session-tool-interrupted:{result.id}:{sealed_id}",
        ) == ([], True)


async def test_mixed_round_keeps_settled_result_and_interrupted_call_on_the_wire(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """A stopped round with one settled call and one cut-off call keeps both:
    the real result next to the fixed notice, and no withheld replacement."""

    import json

    from app.services import session_semantic_history as history

    stopped = await _member_stopped_mid_tool_session(
        owner_sessionmaker,
        monkeypatch,
        prompt="搜索行业动态并总结",
        followup="继续之前的检索",
        calls=[
            {"kind": "settled", "result_content": "industry news digest"},
            {"kind": "fence"},
        ],
    )
    settled_id, cut_off_id = stopped["tool_use_ids"]

    bindings, wire = await _next_turn_model_input(owner_sessionmaker, stopped=stopped)
    contents = [message.get("content") for message in wire]
    assert stopped["plan_text"] in contents, wire
    tool_messages = {message["tool_call_id"]: message["content"] for message in wire if message.get("role") == "tool"}
    assert tool_messages[settled_id] == "industry news digest", wire
    assert tool_messages[cut_off_id] == history._INTERRUPTED_TOOL_CALL_NO_RESULT, wire
    assert history._USER_STOP_MARKER in contents, wire
    assert "无法核实" not in json.dumps(wire, ensure_ascii=False), wire
    assert stopped["receipt"]["coverage"]["settled_tool_results"] == 1, stopped["receipt"]
    assert stopped["receipt"]["held_items"] == [], stopped["receipt"]


async def test_needs_reconciliation_call_keeps_the_stopped_round_held(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """A quarantined effect in a member-stopped run still withholds the whole
    round: the stop pairing never claims an unknown effect is settled."""

    import json

    from app.services import session_semantic_history as history

    stopped = await _member_stopped_mid_tool_session(
        owner_sessionmaker,
        monkeypatch,
        prompt="搜索新闻",
        followup="继续",
        calls=[{"kind": "needs_reconciliation"}],
    )

    held = stopped["receipt"]["held_items"]
    assert any(item["kind"] == "unsettled_tool_round" for item in held), held
    contents = [entry.get("content") for entry in stopped["conversation"]]
    assert stopped["plan_text"] not in contents
    assert history._INTERRUPTED_TOOL_CALL_NEVER_RAN not in contents
    assert history._INTERRUPTED_TOOL_CALL_NO_RESULT not in contents

    bindings, wire = await _next_turn_model_input(owner_sessionmaker, stopped=stopped)
    assert not [
        entry for entry in bindings["messages"] if str(entry.get("source_ref", "")).startswith("session-tool-interrupted:")
    ], bindings["messages"]
    wire_contents = [message.get("content") for message in wire]
    assert stopped["plan_text"] not in wire_contents, wire
    assert "无法核实" not in json.dumps(wire, ensure_ascii=False), wire


async def test_approval_required_with_broken_ticket_keeps_the_stopped_round_held(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """A prepared call that already recorded an approval_required result with a
    failing ticket check stays held: the stop pairing must not overwrite a
    recorded receipt with the never-ran sentence."""

    import json

    from app.models.session_v2 import SessionToolInvocation
    from app.services import session_semantic_history as history

    stopped = await _member_stopped_mid_tool_session(
        owner_sessionmaker,
        monkeypatch,
        prompt="发送通知前准备",
        followup="继续",
        calls=[{"kind": "approval_ticket_broken", "tool": "send_web_message"}],
    )
    tool_use_id, = stopped["tool_use_ids"]

    async with owner_sessionmaker() as db:
        invocation = await db.get(SessionToolInvocation, stopped["invocations"][tool_use_id].id)
        assert invocation.effect_state == "prepared_not_started"
        assert invocation.result_event_id is not None

    held = stopped["receipt"]["held_items"]
    assert any(
        item["kind"] == "unsettled_tool_round" and item["missing_provider_tool_use_ids"] == [tool_use_id]
        for item in held
    ), held
    contents = [entry.get("content") for entry in stopped["conversation"]]
    assert stopped["plan_text"] not in contents
    assert history._INTERRUPTED_TOOL_CALL_NEVER_RAN not in contents


async def test_kill_without_member_stop_keeps_the_round_held(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """No stop control bound to the cancelled run means no stop pairing: the
    dangling round stays held exactly as before this slice."""

    from app.services import session_semantic_history as history

    stopped = await _member_stopped_mid_tool_session(
        owner_sessionmaker,
        monkeypatch,
        prompt="搜索新闻",
        followup="继续",
        calls=[{"kind": "prepared"}],
        member_stop=False,
    )

    held = stopped["receipt"]["held_items"]
    assert any(item["kind"] == "unsettled_tool_round" for item in held), held
    contents = [entry.get("content") for entry in stopped["conversation"]]
    assert stopped["plan_text"] not in contents
    assert history._USER_STOP_MARKER not in contents


async def test_session_tool_interrupted_ref_resolution_rejects_foreign_scope(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """The synthetic ref binds only its own seal, its own issued call, and a
    run cancelled with a stop control — the same resolver group chat
    (shared_group_authority), project references (project_context) and
    compaction catalogues (invocation_trace) read through."""

    from app.models.session_v2 import SessionModelResult
    from app.services.session_model_inputs import _resolve_origin_dependencies

    stopped = await _member_stopped_mid_tool_session(
        owner_sessionmaker,
        monkeypatch,
        prompt="搜索新闻",
        followup="继续",
        calls=[{"kind": "prepared"}],
    )
    search_id, = stopped["tool_use_ids"]

    async with owner_sessionmaker() as db:
        result = await db.scalar(
            select(SessionModelResult).where(SessionModelResult.run_id == stopped["run1"])
        )
        assert result is not None
        ref = f"session-tool-interrupted:{result.id}:{search_id}"
        assert await _resolve_origin_dependencies(
            db, tenant_id=stopped["tenant_id"], agent_id=stopped["agent_id"], ref=ref
        ) == ([], True)
        assert await _resolve_origin_dependencies(
            db, tenant_id=uuid.uuid4(), agent_id=stopped["agent_id"], ref=ref
        ) == ([], False)
        assert await _resolve_origin_dependencies(
            db, tenant_id=stopped["tenant_id"], agent_id=uuid.uuid4(), ref=ref
        ) == ([], False)
        assert await _resolve_origin_dependencies(
            db,
            tenant_id=stopped["tenant_id"],
            agent_id=stopped["agent_id"],
            ref=f"session-tool-interrupted:{result.id}:not-the-issued-call",
        ) == ([], False)
        assert await _resolve_origin_dependencies(
            db,
            tenant_id=stopped["tenant_id"],
            agent_id=stopped["agent_id"],
            ref="session-tool-interrupted:not-a-uuid:x",
        ) == ([], False)
        assert await _resolve_origin_dependencies(
            db,
            tenant_id=stopped["tenant_id"],
            agent_id=stopped["agent_id"],
            ref=f"session-tool-interrupted:{result.id}",
        ) == ([], False)

    stopped_without_control = await _member_stopped_mid_tool_session(
        owner_sessionmaker,
        monkeypatch,
        prompt="搜索新闻",
        followup="继续",
        calls=[{"kind": "prepared"}],
        member_stop=False,
    )
    held_without_control = next(
        item for item in stopped_without_control["receipt"]["held_items"]
        if item["kind"] == "unsettled_tool_round"
    )
    async with owner_sessionmaker() as db:
        result = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == stopped_without_control["run1"]
            )
        )
        assert result is not None
        assert await _resolve_origin_dependencies(
            db,
            tenant_id=stopped_without_control["tenant_id"],
            agent_id=stopped_without_control["agent_id"],
            ref=f"session-tool-interrupted:{result.id}:{held_without_control['missing_provider_tool_use_ids'][0]}",
        ) == ([], False)


async def test_member_stop_with_started_effect_still_accepts_next_member_message(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """Production run13 shape: a killed run's effect_started call must not
    block the member's follow-up at the real start path
    (TOOL_EFFECT_RECONCILIATION_TASK_STATUSES only gates failed and
    needs_reconciliation runs)."""

    stopped = await _member_stopped_mid_tool_session(
        owner_sessionmaker,
        monkeypatch,
        prompt="搜索新闻并写一篇综述",
        followup="刚才做了什么？",
        calls=[{"kind": "fence"}],
    )
    assert stopped["admission"] == "admitted"
    async with owner_sessionmaker() as db:
        from app.models.runtime_task import RuntimeTask

        task = await db.get(RuntimeTask, stopped["run2"])
        assert task is not None and task.status not in {"failed", "needs_reconciliation"}
