"""Lossless browser-safe delivery cursors for durable Session events.

``ChatTranscriptEvent.sequence`` remains the canonical storage/evidence
sequence.  Legacy V1 runtime writers used nanosecond timestamps for that
column; those values are both non-contiguous and larger than JavaScript's
lossless integer range.  Rewriting the durable rows would invalidate T0 and
outbox evidence, so a Session whose entire storage namespace is above that
range receives a derived, dense delivery cursor at the HTTP/WebSocket
boundary.  Mixed safe/unsafe namespaces fail closed.  The original storage
sequence remains present as decimal text on every projected envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_transcript_event import ChatTranscriptEvent


JS_SAFE_SEQUENCE_MAX = (1 << 53) - 1
IDENTITY_SEQUENCE_PROJECTION = "identity"
LEGACY_RANKED_SEQUENCE_PROJECTION = "ranked_unsafe_storage_v1"


class SessionDeliveryCursorError(ValueError):
    """Stored Session ordering cannot be exposed through the V2 wire contract."""


@dataclass(frozen=True, slots=True)
class SessionDeliveryCursor:
    mode: Literal["identity", "ranked_unsafe_storage_v1"]
    event_count: int
    storage_first_sequence: int
    storage_last_sequence: int

    @property
    def last_committed_delivery_sequence(self) -> int:
        if self.mode == IDENTITY_SEQUENCE_PROJECTION:
            return self.storage_last_sequence
        return self.event_count

    @property
    def live_storage_offset(self) -> int:
        """Stable raw-minus-delivery offset for events appended after the snapshot."""

        return self.storage_last_sequence - self.last_committed_delivery_sequence


def resolve_session_delivery_cursor(
    *,
    event_count: int,
    storage_first_sequence: int,
    storage_last_sequence: int,
) -> SessionDeliveryCursor:
    """Resolve the only two accepted storage shapes without semantic guessing."""

    count = max(0, int(event_count))
    first = max(0, int(storage_first_sequence))
    last = max(0, int(storage_last_sequence))
    if count == 0:
        return SessionDeliveryCursor(
            mode=IDENTITY_SEQUENCE_PROJECTION,
            event_count=0,
            storage_first_sequence=0,
            storage_last_sequence=0,
        )
    if first == 1 and last == count and last <= JS_SAFE_SEQUENCE_MAX:
        return SessionDeliveryCursor(
            mode=IDENTITY_SEQUENCE_PROJECTION,
            event_count=count,
            storage_first_sequence=first,
            storage_last_sequence=last,
        )

    ranked_unsafe_storage = (
        first > JS_SAFE_SEQUENCE_MAX and last >= first and last - first + 1 >= count and count <= JS_SAFE_SEQUENCE_MAX
    )
    if ranked_unsafe_storage:
        return SessionDeliveryCursor(
            mode=LEGACY_RANKED_SEQUENCE_PROJECTION,
            event_count=count,
            storage_first_sequence=first,
            storage_last_sequence=last,
        )
    raise SessionDeliveryCursorError("session_delivery_cursor_unrecoverable")


async def load_session_delivery_cursor(
    db: AsyncSession,
    *,
    session_id: uuid.UUID | str,
) -> SessionDeliveryCursor:
    session_uuid = session_id if isinstance(session_id, uuid.UUID) else uuid.UUID(str(session_id))
    stats = (
        await db.execute(
            select(
                func.count(ChatTranscriptEvent.id),
                func.coalesce(func.min(ChatTranscriptEvent.sequence), 0),
                func.coalesce(func.max(ChatTranscriptEvent.sequence), 0),
            ).where(ChatTranscriptEvent.session_id == session_uuid)
        )
    ).one()
    count, first, last = (int(stats[0] or 0), int(stats[1] or 0), int(stats[2] or 0))
    if count == 0 or (first == 1 and last == count and last <= JS_SAFE_SEQUENCE_MAX):
        return resolve_session_delivery_cursor(
            event_count=count,
            storage_first_sequence=first,
            storage_last_sequence=last,
        )

    return resolve_session_delivery_cursor(
        event_count=count,
        storage_first_sequence=first,
        storage_last_sequence=last,
    )


def project_session_event_for_delivery(
    envelope: Mapping[str, Any],
    *,
    cursor: SessionDeliveryCursor,
    storage_sequence: int,
    delivery_sequence: int,
) -> dict[str, Any]:
    """Project one envelope without mutating its canonical storage evidence."""

    storage = int(storage_sequence)
    delivery = int(delivery_sequence)
    if delivery <= 0 or delivery > JS_SAFE_SEQUENCE_MAX:
        raise SessionDeliveryCursorError("session_delivery_cursor_unrecoverable")
    if cursor.mode == IDENTITY_SEQUENCE_PROJECTION:
        if storage != delivery:
            raise SessionDeliveryCursorError("session_delivery_cursor_identity_mismatch")
        return dict(envelope)
    projected = dict(envelope)
    projected["sequence"] = delivery
    projected["storage_sequence"] = str(storage)
    projected["sequence_projection"] = cursor.mode
    return projected


def project_future_session_event_for_delivery(
    envelope: Mapping[str, Any],
    *,
    storage_sequence: int,
    storage_offset: int,
) -> dict[str, Any]:
    """Project a post-watermark live frame with the ranked cursor's stable offset."""

    storage = int(storage_sequence)
    offset = int(storage_offset)
    if offset <= 0:
        return dict(envelope)
    delivery = storage - offset
    if delivery <= 0 or delivery > JS_SAFE_SEQUENCE_MAX:
        raise SessionDeliveryCursorError("session_delivery_cursor_unrecoverable")
    projected = dict(envelope)
    projected["sequence"] = delivery
    projected["storage_sequence"] = str(storage)
    projected["sequence_projection"] = LEGACY_RANKED_SEQUENCE_PROJECTION
    return projected


async def load_session_delivery_events(
    db: AsyncSession,
    *,
    session_id: uuid.UUID | str,
    cursor: SessionDeliveryCursor,
    after_sequence: int = 0,
    before_sequence: int | None = None,
    through_sequence: int | None = None,
    direction: Literal["forward", "backward"] = "forward",
    limit: int = 200,
) -> list[tuple[ChatTranscriptEvent, int]]:
    """Read one page by delivery cursor and always return ascending rows."""

    session_uuid = session_id if isinstance(session_id, uuid.UUID) else uuid.UUID(str(session_id))
    safe_limit = max(1, min(int(limit), 1000))
    if cursor.mode == IDENTITY_SEQUENCE_PROJECTION:
        stmt = select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_uuid)
        if direction == "backward" or before_sequence is not None:
            if before_sequence is not None:
                stmt = stmt.where(ChatTranscriptEvent.sequence < int(before_sequence))
            rows = list((await db.scalars(stmt.order_by(ChatTranscriptEvent.sequence.desc()).limit(safe_limit))).all())
            rows.reverse()
        else:
            stmt = stmt.where(ChatTranscriptEvent.sequence > int(after_sequence))
            if through_sequence is not None:
                stmt = stmt.where(ChatTranscriptEvent.sequence <= int(through_sequence))
            rows = list((await db.scalars(stmt.order_by(ChatTranscriptEvent.sequence.asc()).limit(safe_limit))).all())
        return [(row, int(row.sequence)) for row in rows]

    ranked = (
        select(
            ChatTranscriptEvent.id.label("event_id"),
            func.row_number().over(order_by=ChatTranscriptEvent.sequence.asc()).label("delivery_sequence"),
        )
        .where(ChatTranscriptEvent.session_id == session_uuid)
        .subquery()
    )
    stmt = select(ChatTranscriptEvent, ranked.c.delivery_sequence).join(
        ranked, ranked.c.event_id == ChatTranscriptEvent.id
    )
    if direction == "backward" or before_sequence is not None:
        if before_sequence is not None:
            stmt = stmt.where(ranked.c.delivery_sequence < int(before_sequence))
        result = await db.execute(stmt.order_by(ranked.c.delivery_sequence.desc()).limit(safe_limit))
        rows = list(result.all())
        rows.reverse()
    else:
        stmt = stmt.where(ranked.c.delivery_sequence > int(after_sequence))
        if through_sequence is not None:
            stmt = stmt.where(ranked.c.delivery_sequence <= int(through_sequence))
        result = await db.execute(stmt.order_by(ranked.c.delivery_sequence.asc()).limit(safe_limit))
        rows = list(result.all())
    return [(row, int(delivery_sequence)) for row, delivery_sequence in rows]
