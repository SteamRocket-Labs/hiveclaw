"""Exercise stop -> manual compact -> message with real canonical rows."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionTurnInput
from app.models.user import User
from app.services.session_event_contract import serialize_session_event
from app.services.session_live_input import submit_live_human_input
from app.services.thread_items import build_thread_item
from tests.integration.test_session_compact_v2_semantic_history import (
    _run_compact,
    _seed,
    _seed_completed_v2_turn,
)


@pytest.mark.asyncio
async def test_post_compact_message_starts_or_exposes_why_it_cannot_start(owner_sessionmaker, monkeypatch):
    from app.services.runtime_terminal_boundary_outbox import enqueue_terminal_boundary

    for hold_boundary in (False, True):
        seed = await _seed(owner_sessionmaker)
        turn = await _seed_completed_v2_turn(owner_sessionmaker, seed, streamed=False)
        agent_id, session_id, tenant_id, user_id = seed
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, turn.run_id)
            task.status = "killed"
            boundary = await enqueue_terminal_boundary(db, task=task, event_kind="turn_cancelled",
                agent_id=agent_id, session_id=session_id, terminal_status="killed", authority_ref="runtime_task",
                authority_id=task.id, binding={"terminal_event_id": uuid.uuid4(), "terminal_sequence": 1,
                    "authority_sha256": "a" * 64})
            if not hold_boundary:
                boundary.status = "delivered"
            await db.commit()
        compact = await _run_compact(owner_sessionmaker, seed, monkeypatch, [], arguments={"keep_recent": 1})
        assert compact["action"] == "compacted_context_installed"
        async with owner_sessionmaker() as db:
            row = await db.scalar(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.session_id == session_id, ChatTranscriptEvent.event_type == "session_compact"))
            # Manual compaction has no run/round scope and remains a truthful
            # compatibility event, rather than inventing canonical run authority.
            compact_event = serialize_session_event(row, audience="user")
            assert compact_event["schema"] == "hive.session_event_compatibility"
            assert "run_id" not in compact_event
            assert build_thread_item(row, audience="user").get("run_id") is None
            input_id = uuid.uuid4()
            receipt = await submit_live_human_input(db=db, agent=await db.get(Agent, agent_id),
                user=await db.get(User, user_id), session=await db.get(ChatSession, session_id),
                content="What were the three tasks before compaction?", source="r8a-test",
                input_id=input_id, idempotency_key=f"r8a:{input_id}")
            persisted = await db.get(SessionTurnInput, input_id)
            assert persisted.content_parts_json[0]["text"] == "What were the three tasks before compaction?"
            assert persisted.target_run_id != turn.run_id
            if hold_boundary:
                assert receipt["run"] is None
                assert receipt["reason_code"] == "terminal_boundary_ack_pending", receipt
            else:
                assert receipt["run"]["run_id"]
                assert (await db.get(RuntimeTask, uuid.UUID(receipt["run"]["run_id"]))).status == "pending"


@pytest.mark.asyncio
async def test_manual_compaction_thread_item_does_not_claim_a_task_is_continuing(owner_sessionmaker, monkeypatch):
    seed = await _seed(owner_sessionmaker)
    await _seed_completed_v2_turn(owner_sessionmaker, seed, streamed=False)
    await _run_compact(owner_sessionmaker, seed, monkeypatch, [], arguments={"keep_recent": 1})
    async with owner_sessionmaker() as db:
        row = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == seed[1], ChatTranscriptEvent.event_type == "session_compact"))
        item = build_thread_item(row, audience="user")
        assert item["user_summary"] == "上下文已压缩。"
        assert item.get("run_id") is None


@pytest.mark.asyncio
async def test_child_history_includes_its_bound_terminal_run_despite_session_id_format(owner_sessionmaker):
    from app.services.session_control_plane import _list_runtime_tasks, _runtime_task_payload

    agent_id, session_id, tenant_id, user_id = await _seed(owner_sessionmaker)
    run_id, delegator_id = uuid.uuid4(), uuid.uuid4()
    completed_at = datetime.now(timezone.utc)
    async with owner_sessionmaker() as db:
        db.add(Agent(id=delegator_id, tenant_id=tenant_id, creator_id=user_id, name="Delegator"))
        await db.flush()
        db.add(RuntimeTask(id=run_id, tenant_id=tenant_id, task_type="delegation", status="failed",
            parent_agent_id=delegator_id, child_agent_id=agent_id, parent_session_id=str(uuid.uuid4()),
            child_session_id=session_id.hex, prompt="Delegated work", completed_at=completed_at,
            started_at=completed_at - timedelta(minutes=5), root_user_id=user_id))
        await db.flush()
        session = await db.get(ChatSession, session_id)
        session.session_kind, session.runtime_task_id = "delegation_run", run_id
        await db.commit()
        for session_key in (session_id, str(session_id)):
            tasks = await _list_runtime_tasks(db, agent_id=agent_id, session_id=session_key)
            assert [task.id for task in tasks] == [run_id]
            assert _runtime_task_payload(tasks[0])["status"] == "failed"
            assert _runtime_task_payload(tasks[0])["completed_at"] == completed_at.isoformat()
