from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_transcript_event import ChatTranscriptEvent


def _uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _is_pending_clarification_event(event: Any) -> bool:
    metadata = getattr(event, "metadata_json", None) or {}
    if metadata.get("answered") is True:
        return False
    if metadata.get("tool_name") == "ask_user_question":
        return True
    content = str(getattr(event, "content", "") or "")
    return "ask_user_question" in content or "awaiting_user_clarification" in content


async def mark_latest_pending_clarification_answered(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    answer_event_id: uuid.UUID | str,
    answer_text: str,
    answered_at: datetime | None = None,
) -> bool:
    """Durably mark the latest pending user clarification card as answered.

    The visible answer remains a normal user transcript event. This metadata is
    only the durable UI acknowledgement that prevents replayed ask_user_question
    cards from becoming interactive again after refresh.
    """
    result = await db.execute(
        select(ChatTranscriptEvent)
        .where(
            ChatTranscriptEvent.agent_id == _uuid(agent_id),
            ChatTranscriptEvent.session_id == _uuid(session_id),
            ChatTranscriptEvent.listed_surface == "chat",
            ChatTranscriptEvent.event_type.in_(("tool_call", "tool_result")),
        )
        .order_by(ChatTranscriptEvent.sequence.desc())
        .limit(1)
    )
    event = result.scalar_one_or_none()
    if event is None or not _is_pending_clarification_event(event):
        return False

    metadata = dict(getattr(event, "metadata_json", None) or {})
    if metadata.get("answered") is True:
        return False
    metadata.update(
        {
            "answered": True,
            "answered_by_event_id": str(answer_event_id),
            "answer_text": answer_text,
            "answered_at": (answered_at or datetime.now(timezone.utc)).isoformat(),
        }
    )
    event.metadata_json = metadata
    return True
