"""Typed Session V2 subscribe/ready and canonical catch-up helpers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.services.session_event_contract import serialize_session_event


SESSION_SUBSCRIPTION_SCHEMA_VERSION = 2
SESSION_SUBSCRIPTION_CLOSE_CODES = {
    "auth_failed": 4401,
    "session_forbidden": 4403,
    "session_not_found": 4404,
    "schema_unsupported": 4406,
    "event_store_retryable": 1013,
}


class SessionSubscriptionError(ValueError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SessionSubscribeRequest:
    session_id: uuid.UUID
    after_sequence: int
    schema_version: int
    connection_attempt_id: str


@dataclass(frozen=True, slots=True)
class SessionCatchupWindow:
    last_committed_sequence: int


def _uuid(value: Any) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise SessionSubscriptionError("session_not_found") from exc


def parse_session_subscribe(
    value: Any,
    *,
    expected_session_id: uuid.UUID | str,
) -> SessionSubscribeRequest:
    if not isinstance(value, Mapping) or value.get("type") != "session.subscribe":
        raise SessionSubscriptionError("schema_unsupported")
    if value.get("schema_version") != SESSION_SUBSCRIPTION_SCHEMA_VERSION:
        raise SessionSubscriptionError("schema_unsupported")
    session_id = _uuid(value.get("session_id"))
    if session_id != _uuid(expected_session_id):
        raise SessionSubscriptionError("session_forbidden")
    after_sequence = value.get("after_sequence")
    if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
        raise SessionSubscriptionError("schema_unsupported")
    connection_attempt_id = str(value.get("connection_attempt_id") or "").strip()
    if not connection_attempt_id or len(connection_attempt_id) > 200:
        raise SessionSubscriptionError("schema_unsupported")
    return SessionSubscribeRequest(
        session_id=session_id,
        after_sequence=after_sequence,
        schema_version=SESSION_SUBSCRIPTION_SCHEMA_VERSION,
        connection_attempt_id=connection_attempt_id,
    )


def build_session_ready(
    *,
    session_id: uuid.UUID | str,
    connection_attempt_id: str,
    accepted_after_sequence: int,
    last_committed_sequence: int,
    active_run: Mapping[str, Any] | None,
    subscription_id: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    active = dict(active_run or {})
    return {
        "type": "session.ready",
        "session_id": str(session_id),
        "subscription_id": str(subscription_id or uuid.uuid4()),
        "connection_attempt_id": connection_attempt_id,
        "accepted_after_sequence": int(accepted_after_sequence),
        "last_committed_sequence": int(last_committed_sequence),
        "active_turn_id": str(active.get("turn_id")) if active.get("turn_id") else None,
        "active_run_id": str(active.get("run_id")) if active.get("run_id") else None,
        "run_status": str(active.get("status")) if active.get("status") else None,
        "schema_version": SESSION_SUBSCRIPTION_SCHEMA_VERSION,
    }


def session_subscription_error_frame(error: SessionSubscriptionError) -> dict[str, Any]:
    return {
        "type": "session.error",
        "error": {
            "code": error.code,
            "retryable": error.retryable,
            "message_key": f"session.{error.code}",
        },
    }


async def load_session_catchup_window(
    db: AsyncSession,
    *,
    session_id: uuid.UUID | str,
    after_sequence: int,
) -> SessionCatchupWindow:
    """Capture the watermark after live buffering starts."""

    session_uuid = _uuid(session_id)
    watermark = int(
        (
            await db.scalar(
                select(func.coalesce(func.max(ChatTranscriptEvent.sequence), 0)).where(
                    ChatTranscriptEvent.session_id == session_uuid
                )
            )
        )
        or 0
    )
    if after_sequence > watermark:
        raise SessionSubscriptionError("event_store_retryable", retryable=True)
    return SessionCatchupWindow(last_committed_sequence=watermark)


async def iter_session_catchup_events(
    db: AsyncSession,
    *,
    session_id: uuid.UUID | str,
    after_sequence: int,
    through_sequence: int,
    audience: str,
    page_size: int = 500,
) -> AsyncIterator[dict[str, Any]]:
    """Stream complete catch-up coverage in bounded DB pages without dropping events."""

    session_uuid = _uuid(session_id)
    cursor = int(after_sequence)
    safe_page_size = max(1, min(int(page_size), 1000))
    while cursor < through_sequence:
        rows = list(
            (
                await db.scalars(
                    select(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.session_id == session_uuid,
                        ChatTranscriptEvent.sequence > cursor,
                        ChatTranscriptEvent.sequence <= through_sequence,
                    )
                    .order_by(ChatTranscriptEvent.sequence.asc())
                    .limit(safe_page_size)
                )
            ).all()
        )
        if not rows:
            raise SessionSubscriptionError("event_store_retryable", retryable=True)
        for row in rows:
            if int(row.sequence) != cursor + 1:
                raise SessionSubscriptionError("event_store_retryable", retryable=True)
            cursor = int(row.sequence)
            yield serialize_session_event(row, audience=audience)
