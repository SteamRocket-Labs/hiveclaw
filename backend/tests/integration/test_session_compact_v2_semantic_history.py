"""Session ``/compact`` must feed the summary model canonical semantic truth.

Production B4 evidence (2026-09-09 04:39): on a real completed V2 turn the
compact control event's ``replacement_messages[1]`` was a system JSON
``provider_call_ledger`` debug projection instead of the assistant's reply,
and the generated summary claimed the reply was "not yet delivered" although
the V2 terminal had completed at 04:18:57. The compact path now reuses the
canonical semantic-history reader (``load_session_semantic_history``), so
these tests seed the production round-commit shape — accepted HumanInput
checkpoint, streamed/zero-copy assistant events, ``result_commit.round_committed``,
and the immutable ``SessionModelResult`` seal — plus the web runtime's
persisted debug ledger row, against a real PostgreSQL.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from app.models.tenant import Tenant
from app.models.user import User
from app.services.session_v2_persistence import SessionEventDraft, append_session_events

ASSISTANT_TEXT = "The current amount is 13."


def _run_scope(session_id, turn_id, run_id) -> dict[str, str]:
    return {
        "level": "run",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": turn_id,
        "run_id": str(run_id),
    }


def _round_scope(session_id, turn_id, run_id, round_id) -> dict[str, str]:
    return {
        "level": "round",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": turn_id,
        "run_id": str(run_id),
        "round_id": round_id,
    }


class TurnSeed:
    def __init__(self, seed, turn_id, run_id, round_id, result_id):
        agent_id, session_id, tenant_id, user_id = seed
        self.agent_id, self.session_id, self.tenant_id, self.user_id = agent_id, session_id, tenant_id, user_id
        self.turn_id = turn_id
        self.run_id = run_id
        self.round_id = round_id
        self.result_id = result_id


async def _seed(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Compact V2 Tenant", slug=f"compact-v2-{suffix}"))
        db.add(
            User(
                id=user_id,
                username=f"compact-v2-{suffix}",
                email=f"compact-v2-{suffix}@example.test",
                password_hash="x",
                display_name="Compact V2 Owner",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        await db.flush()
        agent = Agent(
            tenant_id=tenant_id,
            creator_id=user_id,
            owner_user_id=user_id,
            name=f"Compact V2 Agent {suffix}",
            role_description="Runs the compact command regression.",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        session = ChatSession(
            agent_id=agent.id,
            user_id=user_id,
            tenant_id=tenant_id,
            title=f"compact-v2-{suffix}",
            session_kind="human_chat",
            runtime_source="web_chat",
            source_channel="web",
        )
        db.add(session)
        await db.commit()
        return agent.id, session.id, tenant_id, user_id


async def _seed_completed_v2_turn(
    owner_sessionmaker,
    seed,
    *,
    streamed: bool,
) -> TurnSeed:
    """Seed the production shape: accepted HumanInput checkpoint, streamed
    and/or zero-copy assistant events, the ``result_commit.round_committed``
    event with its immutable seal, and the runtime's persisted debug ledger."""
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

    turn_id = f"turn-{uuid.uuid4().hex[:8]}"
    run_id = uuid.uuid4()
    round_id = f"round-{uuid.uuid4().hex[:8]}"
    result_id = uuid.uuid4()
    input_id = uuid.uuid4()
    agent_id, session_id, tenant_id, user_id = seed
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        authority = await resolve_session_mutation_authority(
            db,
            user=user,
            agent_id=agent_id,
            session_id=session_id,
            action="mutate_session_input",
        )
        await accept_human_input(
            db,
            authority=authority,
            intent={
                "kind": "queue_next_turn",
                "input_id": str(input_id),
                "idempotency_key": f"compact-v2-{input_id}",
                "session_id": str(session_id),
                "content_parts": [{"type": "text", "text": "current amount is 13"}],
            },
        )
        db.add(
            RuntimeTask(
                id=run_id,
                task_type="web_chat_turn",
                status="completed",
                parent_agent_id=agent_id,
                child_agent_id=agent_id,
                parent_session_id=str(session_id),
                child_session_id=str(session_id),
                tenant_id=tenant_id,
                root_idempotency_key=f"compact-v2-run-{run_id}",
                metadata_json={"turn_id": turn_id},
            )
        )
        await db.flush()

        # The round-committed immutable provider seal; inserted before the
        # events because the transcript's result-authority check constraint
        # validates every result_id reference at insert time.
        db.add(
            SessionModelResult(
                id=result_id,
                tenant_id=tenant_id,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                round_id=round_id,
                provider_request_id=f"compact-v2-{result_id}",
                state="round_committed",
                model_request_hash="0" * 64,
                model_request_snapshot_json={},
                seal_json={
                    "semantic_content": ASSISTANT_TEXT,
                    "content_hash": "0" * 64,
                    "response": {"content": ASSISTANT_TEXT, "tool_calls": []},
                },
            )
        )
        await db.flush()

        assistant_drafts = [
            SessionEventDraft(
                item_id=uuid.uuid5(run_id, "assistant-final:0"),
                item_kind="assistant_final",
                lifecycle="completed",
                scope=_round_scope(session_id, turn_id, run_id, round_id),
                actor={"type": "assistant", "agent_id": str(agent_id)},
                payload={
                    "phase": "final",
                    "zero_copy": True,
                    "terminal_result_id": str(result_id),
                    "source_blocks": [{"kind": "assistant_final", "content_hash": "0" * 64, "block_index": 0}],
                },
                result_id=result_id,
            ),
        ]
        if streamed:
            # A streamed turn also persisted visible text batches before the
            # zero-copy terminal; compact must still emit exactly ONE
            # assistant message for the round.
            assistant_drafts = [
                SessionEventDraft(
                    item_id=uuid.uuid5(run_id, "assistant-visible-text:0"),
                    item_kind="assistant_text",
                    lifecycle="snapshot",
                    scope=_round_scope(session_id, turn_id, run_id, round_id),
                    actor={"type": "assistant", "agent_id": str(agent_id)},
                    payload={"phase": "unknown", "content": ASSISTANT_TEXT, "block_index": 0},
                    result_id=result_id,
                ),
                *assistant_drafts,
            ]
        await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=[
                SessionEventDraft(
                    item_id=run_id,
                    item_kind="run",
                    lifecycle="completed",
                    scope=_run_scope(session_id, turn_id, run_id),
                    actor={"type": "runtime"},
                    payload={"reason": "completed"},
                ),
                *assistant_drafts,
                SessionEventDraft(
                    item_id=result_id,
                    item_kind="result_commit",
                    lifecycle="round_committed",
                    scope=_round_scope(session_id, turn_id, run_id, round_id),
                    actor={"type": "runtime"},
                    payload={
                        "provider_request_id": f"compact-v2-{result_id}",
                        "continuation_verdict": "final",
                    },
                    result_id=result_id,
                ),
            ],
        )
        # The debug projection the web runtime persists after every model round.
        from app.services.chat_transcript import append_session_event

        await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            actor_type="system",
            event_type="provider_call_ledger",
            role="system",
            user_id=user_id,
            run_id=run_id,
            content=json.dumps(
                {
                    "type": "session_context",
                    "event_type": "provider_call_ledger",
                    "provider_prompt_ledger": {"tool_count": 0},
                    "visibility": "debug",
                }
            ),
            source="web_chat_runtime",
            materialize_chat_message=False,
        )
        await db.commit()
        return TurnSeed(seed, turn_id, run_id, round_id, result_id)


async def _run_compact(owner_sessionmaker, seed, monkeypatch, captured_messages, *, arguments=None):
    from app.services import session_command_runtime
    from app.services.session_command_runtime import SessionCommandContext, execute_session_command

    async def _fake_summary(messages, tenant_id, *, agent_id=None, user_id=None):
        captured_messages.append([dict(message) for message in messages])
        return "Summary of the conversation."

    monkeypatch.setattr(session_command_runtime, "_generate_session_summary", _fake_summary)
    agent_id, session_id, tenant_id, user_id = seed
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        result = await execute_session_command(
            context=SessionCommandContext(
                db=db,
                agent=agent,
                user=user,
                access_level="owner",
                session_id=str(session_id),
                arguments=dict(arguments or {}),
            ),
            command_name="compact",
        )
        await db.commit()
        return result


async def test_compact_zero_copy_v2_round_resolves_reply_and_excludes_debug_rows(
    owner_sessionmaker, monkeypatch
) -> None:
    seed = await _seed(owner_sessionmaker)
    await _seed_completed_v2_turn(owner_sessionmaker, seed, streamed=False)

    captured: list[list[dict[str, str]]] = []
    result = await _run_compact(owner_sessionmaker, seed, monkeypatch, captured, arguments={"keep_recent": 1})

    assert result["action"] == "compacted_context_installed", result
    assert captured == [
        [
            {"role": "user", "content": "current amount is 13"},
            {"role": "assistant", "content": ASSISTANT_TEXT},
        ]
    ]
    replacement = result["debug_payload"]["replacement_messages"]
    assert replacement[-1] == {"role": "assistant", "content": ASSISTANT_TEXT}
    receipt = result["debug_payload"]["semantic_history_receipt"]
    assert receipt["status"] == "complete"
    assert receipt["coverage"]["committed_provider_messages"] == 1


async def test_compact_streamed_plus_terminal_round_emits_single_assistant_message(
    owner_sessionmaker, monkeypatch
) -> None:
    seed = await _seed(owner_sessionmaker)
    await _seed_completed_v2_turn(owner_sessionmaker, seed, streamed=True)

    captured: list[list[dict[str, str]]] = []
    result = await _run_compact(owner_sessionmaker, seed, monkeypatch, captured, arguments={"keep_recent": 1})

    assert result["action"] == "compacted_context_installed", result
    assistant_entries = [m for m in captured[0] if m["role"] == "assistant"]
    assert assistant_entries == [{"role": "assistant", "content": ASSISTANT_TEXT}]
    receipt = result["debug_payload"]["semantic_history_receipt"]
    assert receipt["coverage"]["committed_provider_messages"] == 1


async def test_compact_unavailable_seal_keeps_projection_and_skips_summary_model(
    owner_sessionmaker, monkeypatch
) -> None:
    seed = await _seed(owner_sessionmaker)
    turn = await _seed_completed_v2_turn(owner_sessionmaker, seed, streamed=False)
    # The canonical seal row is genuinely unavailable (e.g. lost/failed state).
    from sqlalchemy import delete

    async with owner_sessionmaker() as db:
        await db.execute(delete(SessionModelResult).where(SessionModelResult.id == turn.result_id))
        await db.commit()

    captured: list[list[dict[str, str]]] = []
    result = await _run_compact(owner_sessionmaker, seed, monkeypatch, captured)

    assert result["ok"] is False
    assert result["action"] == "unavailable", result
    assert captured == [], "summary model must not run over missing canonical evidence"
    assert result["debug_payload"]["error_code"] == "committed_model_seal_unavailable"
    assert result["debug_payload"]["retryable"] is True
    async with owner_sessionmaker() as db:
        session = await db.get(ChatSession, turn.session_id)
        assert "active_projection" not in dict(session.transcript_metadata_json or {}), (
            "the prior projection must remain untouched"
        )


async def test_compact_source_scope_mismatch_fails_closed_without_mutation(owner_sessionmaker, monkeypatch) -> None:
    seed = await _seed(owner_sessionmaker)
    turn = await _seed_completed_v2_turn(owner_sessionmaker, seed, streamed=False)
    # Simulate authority drift the read model must catch: the committed seal's
    # run binding no longer matches the round-commit event's run scope. The
    # binding trigger allows rebinding to another valid task of the same turn.
    drift_run_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=drift_run_id,
                task_type="web_chat_turn",
                status="completed",
                parent_agent_id=turn.agent_id,
                child_agent_id=turn.agent_id,
                parent_session_id=str(turn.session_id),
                child_session_id=str(turn.session_id),
                tenant_id=turn.tenant_id,
                root_idempotency_key=f"compact-v2-drift-{drift_run_id}",
                metadata_json={"turn_id": turn.turn_id},
            )
        )
        await db.flush()
        result_row = await db.get(SessionModelResult, turn.result_id)
        result_row.run_id = drift_run_id
        await db.commit()

    captured: list[list[dict[str, str]]] = []
    result = await _run_compact(owner_sessionmaker, seed, monkeypatch, captured)

    assert result["ok"] is False
    assert result["action"] == "unavailable", result
    assert captured == []
    assert result["debug_payload"]["error_code"] == "committed_model_seal_authority_mismatch"
    assert result["debug_payload"]["retryable"] is False
    async with owner_sessionmaker() as db:
        session = await db.get(ChatSession, turn.session_id)
        assert "active_projection" not in dict(session.transcript_metadata_json or {})


async def test_compact_unsettled_tool_round_keeps_existing_projection(owner_sessionmaker, monkeypatch):
    seed = await _seed(owner_sessionmaker)
    turn = await _seed_completed_v2_turn(owner_sessionmaker, seed, streamed=False)
    prior = {"projection_reason": "compact", "summary": "Preserved prior context"}
    async with owner_sessionmaker() as db:
        result = await db.get(SessionModelResult, turn.result_id)
        result.seal_json = {
            **result.seal_json,
            "response": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "pending-call",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"workspace/report.md"}',
                        },
                    }
                ],
            },
        }
        session = await db.get(ChatSession, turn.session_id)
        session.transcript_metadata_json = {"active_projection": prior}
        await db.commit()
    captured = []
    result = await _run_compact(owner_sessionmaker, seed, monkeypatch, captured)
    assert result["ok"] is False
    assert result["debug_payload"]["error_code"] == "unsettled_semantic_history"
    assert captured == []
    async with owner_sessionmaker() as db:
        session = await db.get(ChatSession, turn.session_id)
        assert session.transcript_metadata_json["active_projection"] == prior


@pytest.mark.parametrize("keep_recent", [1, 2])
async def test_compact_tool_pair_survives_summary_and_projection_consumption(
    owner_sessionmaker,
    monkeypatch,
    keep_recent,
):
    from datetime import datetime, timezone
    from app.services.session_semantic_history import SessionSemanticHistory, SessionSemanticMessage
    from app.services.web_chat_runtime import _apply_active_projection_to_history

    seed = await _seed(owner_sessionmaker)
    call = {
        "id": "read-1",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": '{"path":"workspace/report.md"}',
        },
    }
    expected = [
        {"role": "user", "content": "Read the report."},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "content": "Model-visible report", "tool_call_id": "read-1"},
    ]
    history = SessionSemanticHistory(
        messages=[
            SessionSemanticMessage(
                id=str(index),
                created_at=datetime.now(timezone.utc),
                sequence_start=index + 1,
                sequence_end=index + 1,
                group_id="read-round",
                source_event_ids=(),
                **item,
            )
            for index, item in enumerate(expected)
        ],
        receipt={"status": "complete", "held_items": []},
    )

    async def read_history(*_args, **_kwargs):
        return history

    monkeypatch.setattr("app.services.session_command_runtime.load_session_semantic_history", read_history)
    captured = []
    result = await _run_compact(owner_sessionmaker, seed, monkeypatch, captured, arguments={"keep_recent": keep_recent})
    assert result["ok"] is True
    assert captured == [expected]
    async with owner_sessionmaker() as db:
        session = await db.get(ChatSession, seed[1])
        consumed = await _apply_active_projection_to_history(db, session, [])
    assert consumed[0]["role"] == "system"
    assert consumed[1:] == (expected[1:] if keep_recent == 2 else [])
