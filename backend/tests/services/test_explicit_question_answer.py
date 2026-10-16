"""An unrelated later message is not an answer to a specific pending question."""

from uuid import uuid4

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.conversation_interaction_service import mark_latest_pending_clarification_answered


async def test_unbound_message_never_marks_a_question_answered():
    event = SimpleNamespace(metadata_json={"tool_name": "ask_user_question"})
    result = SimpleNamespace(scalar_one_or_none=lambda: event)
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    assert await mark_latest_pending_clarification_answered(
        db=db, agent_id=uuid4(), session_id=uuid4(), answer_event_id=uuid4(), answer_text="An unrelated update",
    ) is False
    assert "answered" not in event.metadata_json
