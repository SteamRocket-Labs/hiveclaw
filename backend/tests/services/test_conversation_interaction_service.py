from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.chat_transcript_event import ChatTranscriptEvent


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, event):
        self.event = event
        self.commits = 0

    async def execute(self, _stmt):
        return _ScalarResult(self.event)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_marks_latest_clarification_event_answered_durably():
    from app.services.conversation_interaction_service import mark_latest_pending_clarification_answered

    agent_id = uuid4()
    session_id = uuid4()
    answer_event_id = uuid4()
    clarification_event = ChatTranscriptEvent(
        id=uuid4(),
        sequence=10,
        tenant_id=uuid4(),
        agent_id=agent_id,
        session_id=session_id,
        actor_type="tool",
        event_type="tool_result",
        visibility_scope="direct_user",
        listed_surface="chat",
        content='{"status":"awaiting_user_clarification","blocking":true,"questions":[{"question":"Scope?"}]}',
        metadata_json={
            "tool_name": "ask_user_question",
            "role": "tool_call",
            "source": "web_chat_runtime",
        },
    )
    db = _FakeDB(clarification_event)

    updated = await mark_latest_pending_clarification_answered(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        answer_event_id=answer_event_id,
        answer_text="Scope: Mine",
    )

    assert updated is True
    assert clarification_event.metadata_json["answered"] is True
    assert clarification_event.metadata_json["answered_by_event_id"] == str(answer_event_id)
    assert clarification_event.metadata_json["answer_text"] == "Scope: Mine"


@pytest.mark.asyncio
async def test_skips_already_answered_clarification_event():
    from app.services.conversation_interaction_service import mark_latest_pending_clarification_answered

    clarification_event = SimpleNamespace(metadata_json={"answered": True})
    db = _FakeDB(clarification_event)

    updated = await mark_latest_pending_clarification_answered(
        db=db,
        agent_id=uuid4(),
        session_id=uuid4(),
        answer_event_id=uuid4(),
        answer_text="Already handled",
    )

    assert updated is False
    assert clarification_event.metadata_json == {"answered": True}
