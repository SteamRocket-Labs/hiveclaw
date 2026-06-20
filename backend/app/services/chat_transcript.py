"""Single-path chat transcript event writer.

The service creates the durable event first-class surface for UI replay while
bridging every event into the canonical T0 Markdown/XML ledger. ChatMessage and
artifact rows are read models, not a second truth source.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.t0.ledger import T0AppendResult, append_t0_session_event
from app.models.audit import ChatMessage
from app.models.chat_transcript_event import ChatTranscriptEvent


CHAT_MESSAGE_ROLES = {"user", "assistant", "system", "tool_call"}


@dataclass(frozen=True, slots=True)
class AppendSessionEventResult:
    event_id: uuid.UUID
    sequence: int
    message_id: uuid.UUID | None
    transcript_event: ChatTranscriptEvent
    chat_message: ChatMessage | None
    t0_result: T0AppendResult


def _uuid_or_none(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _metadata_with_transcript_refs(
    *,
    metadata: dict[str, Any] | None,
    event_id: uuid.UUID,
    sequence: int,
    parts: list[dict[str, Any]] | None,
    actor_type: str,
    event_type: str,
    role: str | None,
    t0_role: str | None,
    visibility_scope: str,
    listed_surface: str,
    source: str,
) -> dict[str, Any]:
    clean = dict(metadata or {})
    clean["transcript_event_id"] = str(event_id)
    clean["transcript_sequence"] = sequence
    clean["actor_type"] = actor_type
    clean["event_type"] = event_type
    if role:
        clean["role"] = role
    if t0_role:
        clean["t0_role"] = t0_role
    clean.setdefault("source", source)
    clean["visibility_scope"] = visibility_scope
    clean["listed_surface"] = listed_surface
    if parts:
        clean["parts"] = parts
        artifact_ids = [part.get("artifact_id") for part in parts if part.get("type") == "artifact" and part.get("artifact_id")]
        if artifact_ids:
            clean["artifact_ids"] = artifact_ids
    return clean


def _next_sequence() -> int:
    """Return an orderable event sequence without changing T0's own sequence.

    The database enforces `(session_id, sequence)` uniqueness. In production this
    uses nanosecond time as a monotonic-enough sequence source for append order;
    T0 retains its own per-segment append sequence as raw evidence truth.
    """
    return time.time_ns()


async def append_session_event(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str,
    actor_type: str,
    event_type: str,
    content: str | None = "",
    role: str | None = None,
    t0_role: str | None = None,
    user_id: uuid.UUID | str | None = None,
    participant_id: uuid.UUID | str | None = None,
    run_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    message_id: uuid.UUID | str | None = None,
    parent_event_id: uuid.UUID | str | None = None,
    root_session_id: uuid.UUID | str | None = None,
    parent_session_id: uuid.UUID | str | None = None,
    parts: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    visibility_scope: str = "direct_user",
    listed_surface: str = "chat",
    materialize_chat_message: bool = True,
    thinking: str | None = None,
    thinking_signature: str | None = None,
    decision_trace_id: str | None = None,
    source: str = "runtime",
    created_at: datetime | None = None,
) -> AppendSessionEventResult:
    """Append a replayable session event and bridge it into T0.

    The caller owns transaction commit/rollback. WebSocket/notification should be
    emitted only after the caller's durable write succeeds.
    """
    agent_uuid = _uuid_or_none(agent_id)
    tenant_uuid = _uuid_or_none(tenant_id)
    session_uuid = _uuid_or_none(session_id)
    run_uuid = _uuid_or_none(run_id) or _uuid_or_none(runtime_task_id)
    user_uuid = _uuid_or_none(user_id)
    participant_uuid = _uuid_or_none(participant_id)
    message_uuid = _uuid_or_none(message_id)
    event_id = uuid.uuid4()
    sequence = _next_sequence()
    content_text = content or ""

    chat_message: ChatMessage | None = None
    if materialize_chat_message and role in CHAT_MESSAGE_ROLES:
        if message_uuid is None:
            message_uuid = uuid.uuid4()
        chat_message = ChatMessage(
            id=message_uuid,
            agent_id=agent_uuid,
            tenant_id=tenant_uuid,
            user_id=user_uuid or agent_uuid,
            participant_id=participant_uuid,
            role=role,
            content=content_text,
            thinking=thinking,
            thinking_signature=thinking_signature,
            decision_trace_id=decision_trace_id,
            conversation_id=str(session_id),
        )
        db.add(chat_message)

    event_metadata = _metadata_with_transcript_refs(
        metadata=metadata,
        event_id=event_id,
        sequence=sequence,
        parts=parts,
        actor_type=actor_type,
        event_type=event_type,
        role=role,
        t0_role=t0_role,
        visibility_scope=visibility_scope,
        listed_surface=listed_surface,
        source=source,
    )
    transcript_event = ChatTranscriptEvent(
        id=event_id,
        sequence=sequence,
        tenant_id=tenant_uuid,
        agent_id=agent_uuid,
        session_id=session_uuid,
        run_id=run_uuid,
        parent_event_id=_uuid_or_none(parent_event_id),
        root_session_id=_uuid_or_none(root_session_id),
        parent_session_id=_uuid_or_none(parent_session_id),
        message_id=message_uuid,
        actor_type=actor_type,
        event_type=event_type,
        visibility_scope=visibility_scope,
        listed_surface=listed_surface,
        content=content_text,
        parts_json=parts,
        metadata_json=event_metadata,
    )
    db.add(transcript_event)
    if hasattr(db, "flush"):
        await db.flush()

    t0_result = append_t0_session_event(
        agent_id=agent_uuid,
        session_id=session_id,
        event_type=event_type,
        role=t0_role if t0_role is not None else role,
        content=content_text,
        message_id=message_uuid,
        actor_id=user_uuid or agent_uuid,
        tenant_id=tenant_uuid,
        runtime_task_id=run_uuid,
        source=source,
        metadata=event_metadata,
        created_at=created_at,
    )
    return AppendSessionEventResult(
        event_id=event_id,
        sequence=sequence,
        message_id=message_uuid,
        transcript_event=transcript_event,
        chat_message=chat_message,
        t0_result=t0_result,
    )
