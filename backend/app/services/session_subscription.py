"""Typed Session V2 subscribe/ready and canonical catch-up helpers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.session_delivery_cursor import (
    IDENTITY_SEQUENCE_PROJECTION,
    SessionDeliveryCursor,
    SessionDeliveryCursorError,
    load_session_delivery_cursor,
    load_session_delivery_events,
    project_session_event_for_delivery,
)
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
    cursor_mode: str
    schema_version: int
    connection_attempt_id: str


@dataclass(frozen=True, slots=True)
class SessionCatchupWindow:
    last_committed_sequence: int
    last_committed_storage_sequence: int
    cursor: SessionDeliveryCursor


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
    cursor_mode = value.get("cursor_mode", "resume")
    if cursor_mode not in {"resume", "live_tail"}:
        raise SessionSubscriptionError("schema_unsupported")
    if cursor_mode == "live_tail" and after_sequence != 0:
        raise SessionSubscriptionError("schema_unsupported")
    connection_attempt_id = str(value.get("connection_attempt_id") or "").strip()
    if not connection_attempt_id or len(connection_attempt_id) > 200:
        raise SessionSubscriptionError("schema_unsupported")
    return SessionSubscribeRequest(
        session_id=session_id,
        after_sequence=after_sequence,
        cursor_mode=cursor_mode,
        schema_version=SESSION_SUBSCRIPTION_SCHEMA_VERSION,
        connection_attempt_id=connection_attempt_id,
    )


def resolve_subscription_cursor(
    request: SessionSubscribeRequest,
    *,
    last_committed_sequence: int,
) -> int:
    """Choose the authoritative cursor for replay or live-tail bootstrap.

    ``live_tail`` is used only when the client has no safe canonical history
    cursor yet. The server captures the current watermark after registering the
    live buffer, so old history can keep recovering independently without
    dropping any event committed after this fence.
    """

    watermark = max(0, int(last_committed_sequence))
    if request.cursor_mode == "live_tail":
        return watermark
    return request.after_sequence


def build_session_ready(
    *,
    session_id: uuid.UUID | str,
    connection_attempt_id: str,
    accepted_after_sequence: int,
    last_committed_sequence: int,
    active_run: Mapping[str, Any] | None,
    subscription_id: uuid.UUID | str | None = None,
    sequence_projection: str = IDENTITY_SEQUENCE_PROJECTION,
) -> dict[str, Any]:
    active = dict(active_run or {})
    ready = {
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
    if sequence_projection != IDENTITY_SEQUENCE_PROJECTION:
        ready["sequence_projection"] = sequence_projection
    return ready


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
    try:
        cursor = await load_session_delivery_cursor(db, session_id=session_uuid)
    except SessionDeliveryCursorError as exc:
        raise SessionSubscriptionError("session_delivery_cursor_unrecoverable") from exc
    delivery_watermark = cursor.last_committed_delivery_sequence
    if after_sequence > delivery_watermark:
        raise SessionSubscriptionError("event_store_retryable", retryable=True)
    return SessionCatchupWindow(
        last_committed_sequence=delivery_watermark,
        last_committed_storage_sequence=cursor.storage_last_sequence,
        cursor=cursor,
    )


async def iter_session_catchup_events(
    db: AsyncSession,
    *,
    session_id: uuid.UUID | str,
    after_sequence: int,
    through_sequence: int,
    cursor: SessionDeliveryCursor,
    audience: str,
    page_size: int = 500,
) -> AsyncIterator[dict[str, Any]]:
    """Stream complete catch-up coverage in bounded DB pages without dropping events."""

    session_uuid = _uuid(session_id)
    delivery_cursor = int(after_sequence)
    safe_page_size = max(1, min(int(page_size), 1000))
    while delivery_cursor < through_sequence:
        rows = await load_session_delivery_events(
            db,
            session_id=session_uuid,
            cursor=cursor,
            after_sequence=delivery_cursor,
            through_sequence=through_sequence,
            direction="forward",
            limit=safe_page_size,
        )
        if not rows:
            raise SessionSubscriptionError("event_store_retryable", retryable=True)
        for row, delivery_sequence in rows:
            if delivery_sequence != delivery_cursor + 1:
                raise SessionSubscriptionError("event_store_retryable", retryable=True)
            delivery_cursor = delivery_sequence
            yield project_session_event_for_delivery(
                serialize_session_event(row, audience=audience),
                cursor=cursor,
                storage_sequence=int(row.sequence),
                delivery_sequence=delivery_sequence,
            )
