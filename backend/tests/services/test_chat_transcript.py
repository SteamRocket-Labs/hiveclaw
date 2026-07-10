from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeDB:
    def __init__(self):
        self.added = []
        self.flushed = 0

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed += 1


@pytest.mark.asyncio
async def test_append_session_event_writes_typed_transcript_and_queues_t0_projection(monkeypatch, tmp_path):
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.audit import ChatMessage
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.chat_transcript import append_session_event

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    message_id = uuid4()
    participant_id = uuid4()
    created_at = datetime(2026, 6, 20, 12, 30, tzinfo=timezone.utc)
    db = _FakeDB()
    published = []

    async def fake_publish_transcript_t0_bridge(**kwargs):
        published.append(kwargs)

    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(
        "app.services.runtime_control_bus.publish_transcript_t0_bridge",
        fake_publish_transcript_t0_bridge,
    )

    result = await append_session_event(
        db=db,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
        actor_type="assistant",
        event_type="assistant_message",
        role="assistant",
        user_id=user_id,
        participant_id=participant_id,
        message_id=message_id,
        content="final answer",
        metadata={"source": "test", "turn_id": "turn-1"},
        created_at=created_at,
    )

    chat_messages = [item for item in db.added if isinstance(item, ChatMessage)]
    transcript_events = [item for item in db.added if isinstance(item, ChatTranscriptEvent)]
    assert len(chat_messages) == 1
    assert chat_messages[0].id == message_id
    assert chat_messages[0].content == "final answer"
    assert chat_messages[0].created_at == created_at
    assert chat_messages[0].participant_id == participant_id
    assert len(transcript_events) == 1
    assert transcript_events[0].id == result.event_id
    assert transcript_events[0].message_id == message_id
    assert transcript_events[0].event_type == "assistant_message"
    assert transcript_events[0].sequence == result.sequence
    assert transcript_events[0].created_at == created_at
    assert transcript_events[0].schema_version == 1
    assert transcript_events[0].item_type == "agent_message"
    assert transcript_events[0].item_status == "succeeded"
    assert transcript_events[0].turn_id == "turn-1"
    assert transcript_events[0].projection_status == "pending"

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert events == []
    assert published == [
        {
            "transcript_event_id": result.event_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "tenant_id": tenant_id,
        }
    ]
    assert transcript_events[0].metadata_json["t0_bridge_pending"] is True


@pytest.mark.asyncio
async def test_append_session_event_queues_non_message_runtime_event_for_t0(monkeypatch, tmp_path):
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.audit import ChatMessage
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.chat_transcript import append_session_event

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    db = _FakeDB()
    published = []

    async def fake_publish_transcript_t0_bridge(**kwargs):
        published.append(kwargs)

    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(
        "app.services.runtime_control_bus.publish_transcript_t0_bridge",
        fake_publish_transcript_t0_bridge,
    )

    await append_session_event(
        db=db,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        actor_type="system",
        event_type="run_completed",
        role=None,
        content="run completed",
        materialize_chat_message=False,
        metadata={"status": "completed"},
    )

    assert not [item for item in db.added if isinstance(item, ChatMessage)]
    transcript_events = [item for item in db.added if isinstance(item, ChatTranscriptEvent)]
    assert len(transcript_events) == 1
    assert transcript_events[0].item_type == "boundary"
    assert transcript_events[0].item_status == "succeeded"
    assert transcript_events[0].projection_status == "pending"
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert events == []
    assert len(published) == 1


@pytest.mark.asyncio
async def test_append_session_event_can_skip_t0_bridge_for_projection(monkeypatch, tmp_path):
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.chat_transcript import append_session_event

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    db = _FakeDB()

    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    result = await append_session_event(
        db=db,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        actor_type="assistant",
        event_type="assistant_message",
        role="assistant",
        content="copied answer from the source branch",
        metadata={"projection_only": True, "semantic_memory_eligible": False},
        source="conversation_branch",
        bridge_to_t0=False,
    )

    transcript_events = [item for item in db.added if isinstance(item, ChatTranscriptEvent)]
    assert len(transcript_events) == 1
    assert transcript_events[0].content == "copied answer from the source branch"
    assert transcript_events[0].metadata_json["projection_only"] is True
    assert transcript_events[0].metadata_json["semantic_memory_eligible"] is False
    assert transcript_events[0].projection_status == "not_requested"
    assert result.t0_result is None
    assert replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path) == []


@pytest.mark.asyncio
async def test_append_session_event_all_roles_use_committed_t0_projection(monkeypatch, tmp_path):
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.chat_transcript_event import ChatTranscriptEvent
    import app.services.chat_transcript as transcript

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    db = _FakeDB()
    published = []

    async def fake_publish_transcript_t0_bridge(**kwargs):
        published.append(kwargs)

    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(
        "app.services.runtime_control_bus.publish_transcript_t0_bridge",
        fake_publish_transcript_t0_bridge,
    )

    result = await transcript.append_session_event(
        db=db,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        actor_type="system",
        event_type="permission_resolved",
        role="system",
        content="permission resolved",
        metadata={"source": "api-role"},
        source="web",
    )

    transcript_events = [item for item in db.added if isinstance(item, ChatTranscriptEvent)]
    assert len(transcript_events) == 1
    assert result.t0_result is None
    assert replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path) == []
    assert published == [
        {
            "transcript_event_id": result.event_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "tenant_id": tenant_id,
        }
    ]
    assert transcript_events[0].metadata_json["t0_bridge_pending"] is True


def test_transcript_item_contract_is_typed_and_vendor_neutral() -> None:
    from app.services.chat_transcript import build_transcript_item_contract

    assert build_transcript_item_contract(event_type="assistant_message", role="assistant", metadata={}) == (
        "agent_message",
        "succeeded",
    )
    assert build_transcript_item_contract(
        event_type="permission_request",
        role="system",
        metadata={"status": "pending"},
    ) == ("approval_request", "waiting_user")
    assert build_transcript_item_contract(
        event_type="workflow_failed",
        role="system",
        metadata={"status": "failed"},
    ) == ("workflow_activity", "failed")
