"""At-least-once publisher for canonical Session V2 event envelopes."""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, enter_rls_bypass
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.session_v2 import SessionEventOutbox


PublishCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ClaimedSessionEvent:
    outbox_id: uuid.UUID
    agent_id: uuid.UUID
    event_id: uuid.UUID
    session_id: uuid.UUID
    sequence: int
    envelope_sha256: str
    envelope: dict[str, Any]
    attempt: int

    def delivery_payload(self) -> dict[str, Any]:
        return {
            "schema": "hive.session_event.delivery",
            "schema_version": 1,
            "event_id": str(self.event_id),
            "agent_id": str(self.agent_id),
            "session_id": str(self.session_id),
            "sequence": self.sequence,
            "envelope_ref": f"session-event:{self.event_id}",
            "envelope_sha256": self.envelope_sha256,
            "envelope": self.envelope,
        }


class SessionEventOutboxPublisher:
    """Claim, publish, retry, and dead-letter canonical event envelopes.

    Redis Pub/Sub is at-least-once here. A crash after publish and before the
    database acknowledgement intentionally republishes the same ``event_id``;
    consumers must deduplicate by that immutable identity.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 2,
        max_attempts: int = 8,
    ) -> None:
        self._session_factory = session_factory or async_session
        self._lease_seconds = max(1, int(lease_seconds))
        self._retry_base_seconds = max(0, int(retry_base_seconds))
        self._max_attempts = max(1, int(max_attempts))

    @contextlib.asynccontextmanager
    async def _worker_session(self, operation: str):
        async with self._session_factory() as db:
            async with enter_rls_bypass(
                db,
                reason=f"session_event_outbox.{operation}",
            ) as bypass_db:
                yield bypass_db

    async def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        limit: int = 100,
        tenant_id: uuid.UUID | None = None,
    ) -> list[ClaimedSessionEvent]:
        current = now or datetime.now(UTC)
        async with self._worker_session("claim") as db:
            statement = (
                select(SessionEventOutbox, ChatTranscriptEvent.agent_id)
                .join(
                    ChatTranscriptEvent,
                    ChatTranscriptEvent.id == SessionEventOutbox.event_id,
                )
                .where(
                    or_(
                        and_(
                            SessionEventOutbox.status == "pending",
                            SessionEventOutbox.available_at <= current,
                        ),
                        and_(
                            SessionEventOutbox.status == "publishing",
                            SessionEventOutbox.claim_expires_at <= current,
                        ),
                    )
                )
            )
            if tenant_id is not None:
                statement = statement.where(SessionEventOutbox.tenant_id == tenant_id)
            rows = list(
                (
                    await db.execute(
                        statement.order_by(SessionEventOutbox.available_at, SessionEventOutbox.sequence)
                        .limit(max(1, int(limit)))
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            claimed: list[ClaimedSessionEvent] = []
            for row, agent_id in rows:
                row.status = "publishing"
                row.claimed_by = str(worker_id)
                row.claim_expires_at = current + timedelta(seconds=self._lease_seconds)
                row.attempts = int(row.attempts or 0) + 1
                claimed.append(
                    ClaimedSessionEvent(
                        outbox_id=row.id,
                        agent_id=agent_id,
                        event_id=row.event_id,
                        session_id=row.session_id,
                        sequence=int(row.sequence),
                        envelope_sha256=row.envelope_sha256,
                        envelope=dict(row.envelope_json or {}),
                        attempt=int(row.attempts),
                    )
                )
            await db.commit()
            return claimed

    @staticmethod
    def _validate_claim(item: ClaimedSessionEvent) -> None:
        envelope = item.envelope
        if str(envelope.get("event_id") or "") != str(item.event_id):
            raise ValueError("session_event_outbox_event_id_mismatch")
        if str((envelope.get("scope") or {}).get("session_id") or "") != str(item.session_id):
            raise ValueError("session_event_outbox_session_id_mismatch")
        if int(envelope.get("sequence") or 0) != item.sequence:
            raise ValueError("session_event_outbox_sequence_mismatch")
        if _sha256(envelope) != item.envelope_sha256:
            raise ValueError("session_event_outbox_envelope_hash_mismatch")

    async def _mark_published(self, *, item: ClaimedSessionEvent, worker_id: str) -> bool:
        async with self._worker_session("ack") as db:
            row = await db.scalar(
                select(SessionEventOutbox).where(SessionEventOutbox.id == item.outbox_id).with_for_update()
            )
            if row is None or row.status != "publishing" or row.claimed_by != str(worker_id):
                return False
            row.status = "published"
            row.published_at = datetime.now(UTC)
            row.last_error = None
            row.claimed_by = None
            row.claim_expires_at = None
            await db.commit()
            return True

    async def _mark_failed(
        self,
        *,
        item: ClaimedSessionEvent,
        worker_id: str,
        error: Exception,
    ) -> str:
        now = datetime.now(UTC)
        async with self._worker_session("retry") as db:
            row = await db.scalar(
                select(SessionEventOutbox).where(SessionEventOutbox.id == item.outbox_id).with_for_update()
            )
            if row is None or row.status != "publishing" or row.claimed_by != str(worker_id):
                return "stale"
            row.last_error = f"{type(error).__name__}: {error}"[:4000]
            row.claimed_by = None
            row.claim_expires_at = None
            if int(row.attempts or 0) >= self._max_attempts:
                # ``failed`` is terminal dead-letter in this table. Retryable
                # attempts return to ``pending`` with a future available_at.
                row.status = "failed"
                outcome = "dead_letter"
            else:
                row.status = "pending"
                delay = self._retry_base_seconds * (2 ** max(0, int(row.attempts or 1) - 1))
                row.available_at = now + timedelta(seconds=delay)
                outcome = "retry"
            await db.commit()
            return outcome

    async def drain_once(
        self,
        *,
        worker_id: str,
        publish_callback: PublishCallback | None = None,
        limit: int = 100,
        tenant_id: uuid.UUID | None = None,
    ) -> dict[str, int]:
        if publish_callback is None:
            from app.services.web_chat_stream_bus import publish_canonical_session_event

            publish_callback = publish_canonical_session_event
        items = await self.claim_batch(worker_id=worker_id, limit=limit, tenant_id=tenant_id)
        counts = {"claimed": len(items), "published": 0, "retried": 0, "dead_lettered": 0}
        for item in items:
            try:
                self._validate_claim(item)
                await publish_callback(item.delivery_payload())
                if await self._mark_published(item=item, worker_id=worker_id):
                    counts["published"] += 1
            except Exception as exc:  # noqa: BLE001 - durable retry owns transport failures.
                outcome = await self._mark_failed(item=item, worker_id=worker_id, error=exc)
                if outcome == "retry":
                    counts["retried"] += 1
                elif outcome == "dead_letter":
                    counts["dead_lettered"] += 1
        return counts
