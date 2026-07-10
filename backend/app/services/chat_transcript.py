"""Single-path chat transcript event writer.

``ChatTranscriptEvent`` is the transactional cloud event truth used for run
recovery and UI replay.  T0 remains the canonical portable Memory evidence
artifact and is projected exactly-once from committed transcript rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.t0.ledger import T0AppendResult
from app.models.audit import ChatMessage
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.services.thread_items import classify_thread_item, classify_thread_item_status


CHAT_MESSAGE_ROLES = {"user", "assistant", "system", "tool_call"}


@dataclass(frozen=True, slots=True)
class AppendSessionEventResult:
    event_id: uuid.UUID
    sequence: int
    message_id: uuid.UUID | None
    transcript_event: ChatTranscriptEvent
    chat_message: ChatMessage | None
    t0_result: T0AppendResult | None


def _uuid_or_none(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


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
        artifact_ids = [
            part.get("artifact_id") for part in parts if part.get("type") == "artifact" and part.get("artifact_id")
        ]
        if artifact_ids:
            clean["artifact_ids"] = artifact_ids
    return clean


async def allocate_transcript_sequence(db: AsyncSession, *, session_id: uuid.UUID) -> int:
    """Allocate a collision-free sequence inside the caller's DB transaction.

    PostgreSQL workers serialize per session with a transaction advisory lock.
    The following ``MAX + 1`` therefore remains monotonic for the session while
    preserving all existing sequence values during migration.
    """
    if not isinstance(db, AsyncSession):
        # Narrow compatibility for unit-test recording sessions; production
        # AsyncSession always takes the DB-serialized path below.
        return uuid.uuid4().int & ((1 << 63) - 1)
    bind = db.get_bind() if hasattr(db, "get_bind") else None
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect_name == "postgresql":
        lock_key = int.from_bytes(session_id.bytes[:8], byteorder="big", signed=True)
        await db.execute(select(func.pg_advisory_xact_lock(lock_key)))
    result = await db.execute(
        select(func.coalesce(func.max(ChatTranscriptEvent.sequence), 0) + 1).where(
            ChatTranscriptEvent.session_id == session_id
        )
    )
    return int(result.scalar_one())


def build_transcript_item_contract(
    *,
    event_type: str,
    role: str | None,
    metadata: dict[str, Any] | None,
) -> tuple[str, str]:
    """Map runtime events into a small vendor-neutral ThreadItem contract."""
    data = dict(metadata or {})
    item_type = classify_thread_item(event_type=event_type, role=role)
    item_status = classify_thread_item_status(item_type=item_type, event_type=event_type, metadata=data)
    return item_type, item_status


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
    data_root: Path | str | None = None,
    bridge_to_t0: bool = True,
) -> AppendSessionEventResult:
    """Append a replayable session event and optionally bridge it into T0.

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
    if session_uuid is None:
        raise ValueError(f"session_id must be a UUID for transcript persistence: {session_id!r}")
    sequence = await allocate_transcript_sequence(db, session_id=session_uuid)
    content_text = content or ""
    item_type, item_status = build_transcript_item_contract(
        event_type=event_type,
        role=role,
        metadata=metadata,
    )

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
        if created_at is not None:
            chat_message.created_at = created_at
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
        schema_version=1,
        item_type=item_type,
        item_status=item_status,
        turn_id=str((metadata or {}).get("turn_id") or "").strip() or None,
        causation_id=_uuid_or_none((metadata or {}).get("causation_id")) or _uuid_or_none(parent_event_id),
        correlation_id=_uuid_or_none((metadata or {}).get("correlation_id")) or run_uuid,
        actor_type=actor_type,
        event_type=event_type,
        visibility_scope=visibility_scope,
        listed_surface=listed_surface,
        content=content_text,
        parts_json=parts,
        metadata_json=event_metadata,
        projection_status="pending" if bridge_to_t0 else "not_requested",
        projection_attempts=0,
    )
    if created_at is not None:
        transcript_event.created_at = created_at
    db.add(transcript_event)
    if hasattr(db, "flush"):
        await db.flush()

    t0_result: T0AppendResult | None = None
    if bridge_to_t0:
        event_metadata = {**event_metadata, "t0_bridge_pending": True}
        transcript_event.metadata_json = event_metadata
        try:
            from app.services.runtime_control_bus import publish_transcript_t0_bridge

            await publish_transcript_t0_bridge(
                transcript_event_id=event_id,
                agent_id=agent_uuid,
                session_id=session_uuid,
                tenant_id=tenant_uuid,
            )
        except Exception as exc:  # noqa: BLE001 - transcript stays durable; runtime sweep/ops can observe pending state.
            projection_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            event_metadata = {
                **event_metadata,
                "t0_bridge_publish_error": projection_error,
            }
            transcript_event.metadata_json = event_metadata
            transcript_event.projection_error = projection_error
    return AppendSessionEventResult(
        event_id=event_id,
        sequence=sequence,
        message_id=message_uuid,
        transcript_event=transcript_event,
        chat_message=chat_message,
        t0_result=t0_result,
    )
