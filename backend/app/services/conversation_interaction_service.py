from __future__ import annotations

import json
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
    answer_event: Any | None = None,
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
    from app.runtime.hooks import HookEvent, emit_hook

    hook_metadata = {
        "tenant_id": str(getattr(event, "tenant_id", "") or "") or None,
        "answer_event_id": str(answer_event_id),
        "clarification_event_id": str(getattr(event, "id", "") or "") or None,
        "questions": metadata.get("questions") or [],
        "original_answer_present": bool(answer_text),
    }
    hook_result = await emit_hook(
        HookEvent.ELICITATION_RESULT,
        evidence_db=db,
        agent_id=agent_id,
        session_id=str(session_id),
        prompt=answer_text,
        source="conversation_interaction",
        metadata=hook_metadata,
    )
    effective_answer = answer_text
    if hook_result and hook_result.elicitation_action == "accept" and hook_result.elicitation_content is not None:
        content = hook_result.elicitation_content
        if isinstance(content, str):
            effective_answer = content
        elif isinstance(content, dict) and isinstance(content.get("answer"), str):
            effective_answer = str(content["answer"])
        else:
            effective_answer = json.dumps(content, ensure_ascii=False, sort_keys=True)
    if answer_event is not None:
        transcript_event = getattr(answer_event, "transcript_event", None)
        if transcript_event is not None:
            answer_metadata = dict(getattr(transcript_event, "metadata_json", None) or {})
            answer_metadata.update(
                {
                    "elicitation_original_answer": answer_text,
                    "elicitation_effective_answer": effective_answer,
                    "elicitation_action": getattr(hook_result, "elicitation_action", None) or "noop",
                }
            )
            transcript_event.metadata_json = answer_metadata
        # ChatMessage/T0 retain the user's mechanical input. The transformed
        # answer is a governed model-input projection carried in metadata and
        # RuntimeTask.prompt, never a rewrite of raw user evidence.
    metadata.update(
        {
            "answered": True,
            "answered_by_event_id": str(answer_event_id),
            "answer_text": effective_answer,
            "original_answer_text": answer_text,
            "elicitation_action": getattr(hook_result, "elicitation_action", None) or "noop",
            "answered_at": (answered_at or datetime.now(timezone.utc)).isoformat(),
        }
    )
    event.metadata_json = metadata
    return True
