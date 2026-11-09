import uuid

import pytest
from sqlalchemy import select, text

from tests.services.test_session_v2_persistence import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("check", ["null_merge", "has_key"])
async def test_transcript_jsonb_keeps_native_operations(owner_sessionmaker, check):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.runtime_control_bus import _merge_transcript_bridge_metadata

    tenant, _, agent, session = await _seed_session(owner_sessionmaker)
    event_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(ChatTranscriptEvent(id=event_id, tenant_id=tenant, agent_id=agent, session_id=session,
            sequence=1, actor_type="tool", event_type="tool_result", content="ordinary",
            metadata_json={"needle": True}))
        await db.commit()
        if check == "null_merge":
            await db.execute(text("UPDATE chat_transcript_events SET metadata_json=NULL WHERE id=:id"), {"id": event_id})
            await db.commit()
            event = await db.get(ChatTranscriptEvent, event_id)
            await _merge_transcript_bridge_metadata(db, event, {"t0_bridge_pending": False})
            await db.commit()
            assert await db.scalar(text("SELECT metadata_json->>'t0_bridge_pending' FROM chat_transcript_events WHERE id=:id"), {"id": event_id}) == "false"
        else:
            assert await db.scalar(select(ChatTranscriptEvent.id).where(
                ChatTranscriptEvent.id == event_id, ChatTranscriptEvent.metadata_json.has_key("needle"))) == event_id


@pytest.mark.parametrize("permanent", [False, True])
async def test_only_oversized_t0_projection_failures_stop_automatic_retry(owner_sessionmaker, monkeypatch, permanent):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services import runtime_control_bus as bus

    tenant, _, agent, session = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        db.add(ChatTranscriptEvent(tenant_id=tenant, agent_id=agent, session_id=session,
            sequence=1, actor_type="tool", event_type="tool_result", content="ordinary",
            metadata_json={}, projection_status="failed", projection_error=(
                "T0LedgerIntegrityError: t0_record_too_large: unavailable" if permanent else "OSError: temporarily unavailable")))
        await db.commit()
    attempted = []

    async def bridge(**kwargs):
        attempted.append(kwargs["transcript_event_id"])
        return False

    monkeypatch.setattr(bus, "bridge_transcript_event_to_t0", bridge)
    await bus.sweep_pending_transcript_t0_bridges(session_id=session)
    assert len(attempted) == (0 if permanent else 1)


@pytest.mark.parametrize("permanent", [False, True])
async def test_new_input_finishes_closed_only_for_permanently_oversized_t0(owner_sessionmaker, permanent):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.models.user import User
    from app.services.session_live_input import submit_live_human_input
    from app.services.runtime_terminal_boundary_outbox import enqueue_terminal_boundary
    from tests.services.test_session_input_control_v2 import _seed_session

    tenant, user, agent, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run)
        task.status = "completed"
        await enqueue_terminal_boundary(db, task=task, event_kind="turn_stop", agent_id=agent,
            session_id=session, terminal_status="completed", authority_ref="runtime_task", authority_id=run,
            binding={"terminal_event_id": uuid.uuid4(), "terminal_sequence": 1, "authority_sha256": "a" * 64})
        db.add(ChatTranscriptEvent(tenant_id=tenant, agent_id=agent, session_id=session,
            sequence=1, actor_type="tool", event_type="tool_result", content="ordinary", metadata_json={},
            projection_status="failed", projection_error=(
                "T0LedgerIntegrityError: t0_record_too_large: unavailable" if permanent else "OSError: temporary")))
        await db.commit()
        receipt = await submit_live_human_input(db=db, agent=await db.get(Agent, agent),
            user=await db.get(User, user), session=await db.get(ChatSession, session), content="Continue",
            source="web", input_id=uuid.uuid4(), idempotency_key=str(uuid.uuid4()))
        assert receipt["dispatch"]["kind"] == ("rejected" if permanent else "deferred")
        row = await db.get(SessionTurnInput, uuid.UUID(receipt["input_id"]))
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == row.id))
        assert admission.state == "admitted"
        assert admission.dispatch_state == ("dispatched" if permanent else "pending")
        assert row.status == ("rejected" if permanent else "accepted")
        if permanent:
            assert receipt["dispatch"]["retryable"] is False
            assert receipt["dispatch"]["message"] == "这段对话的内容不完整，请新建对话后重试。"
