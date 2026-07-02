"""Unified session/thread index read model."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _metadata(session: ChatSession) -> dict[str, Any]:
    return dict(getattr(session, "transcript_metadata_json", None) or {})


def _event_checkpoint(event: ChatTranscriptEvent) -> dict[str, Any] | None:
    if event.event_type != "user_message":
        return None
    return {
        "checkpoint_event_id": str(event.id),
        "sequence": int(event.sequence),
        "content_preview": (event.content or "")[:240],
        "created_at": event.created_at.isoformat() if getattr(event, "created_at", None) else None,
    }


def _active_projection(meta: dict[str, Any]) -> dict[str, Any] | None:
    projection = meta.get("active_projection")
    if not isinstance(projection, dict):
        return None
    reason = str(projection.get("projection_reason") or projection.get("command") or "").strip()
    checkpoint_event_id = projection.get("checkpoint_event_id") or projection.get("anchor_event_id")
    if not reason and not checkpoint_event_id:
        return None
    return {
        "projection_reason": reason or None,
        "checkpoint_event_id": str(checkpoint_event_id) if checkpoint_event_id else None,
        "applied_at": projection.get("applied_at"),
    }


def build_session_index(
    *,
    session: ChatSession,
    transcript_events: list[ChatTranscriptEvent] | None = None,
    t0_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    meta = _metadata(session)
    events = sorted(transcript_events or [], key=lambda item: int(item.sequence))
    checkpoints = [checkpoint for event in events if (checkpoint := _event_checkpoint(event)) is not None]
    dynamic_tools = list(meta.get("dynamic_tools") or meta.get("available_deferred_tools") or [])
    indexed_t0_segments = list(t0_segments if t0_segments is not None else meta.get("t0_segments") or [])
    return {
        "schema": "hive.session_index.v1",
        "thread_id": str(session.id),
        "session_id": str(session.id),
        "agent_id": str(session.agent_id),
        "tenant_id": _str_or_none(session.tenant_id),
        "user_id": str(session.user_id),
        "title": session.title,
        "source": session.source_channel,
        "thread_source": session.runtime_source,
        "listed_surface": session.listed_surface,
        "visibility_scope": session.visibility_scope,
        "session_kind": session.session_kind,
        "forked_from_id": meta.get("forked_from_id") or _str_or_none(session.parent_session_id),
        "parent_thread_id": _str_or_none(session.parent_session_id),
        "root_thread_id": _str_or_none(session.root_session_id or session.id),
        "runtime_task_id": _str_or_none(session.runtime_task_id),
        "event_persistence_mode": str(meta.get("event_persistence_mode") or "extended"),
        "dynamic_tools": dynamic_tools,
        "memory_mode": meta.get("memory_mode"),
        "archived": bool(meta.get("archived", False)),
        "created_at": session.created_at.isoformat() if getattr(session, "created_at", None) else None,
        "last_message_at": session.last_message_at.isoformat() if getattr(session, "last_message_at", None) else None,
        "checkpoints": checkpoints,
        # Active context projection (rewind/compact soft-projection): the
        # transcript keeps every event; this tells readers which checkpoint is
        # the projected head so tails can render as rewound, not vanish.
        "active_projection": _active_projection(meta),
        "last_event_sequence": int(events[-1].sequence) if events else None,
        "event_count": len(events),
        "t0_segments": indexed_t0_segments,
        "resume_health": {
            "has_t0_truth": bool(indexed_t0_segments),
            "has_checkpoints": bool(checkpoints),
            "truth_surface": "events.jsonl",
        },
    }


async def read_session_index(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
) -> dict[str, Any] | None:
    session_result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id).limit(1)
    )
    session = session_result.scalar_one_or_none()
    if session is None:
        return None
    events_result = await db.execute(
        select(ChatTranscriptEvent)
        .where(ChatTranscriptEvent.session_id == session_id)
        .order_by(ChatTranscriptEvent.sequence.asc())
    )
    events = list(events_result.scalars().all())
    return build_session_index(session=session, transcript_events=events)
