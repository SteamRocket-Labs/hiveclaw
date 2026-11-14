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

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, enter_rls_bypass
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.session_v2 import SessionEventOutbox


PublishCallback = Callable[[dict[str, Any]], Awaitable[None]]


def session_event_claim_candidate_statement(*, now: datetime):
    return select(SessionEventOutbox.id).where(
        or_(
            and_(SessionEventOutbox.status == "pending", SessionEventOutbox.available_at <= now),
            and_(SessionEventOutbox.status == "publishing", SessionEventOutbox.claim_expires_at <= now),
        )
    )


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
            statement = session_event_claim_candidate_statement(now=current)
            if tenant_id is not None:
                statement = statement.where(SessionEventOutbox.tenant_id == tenant_id)
            rows = list(
                (
                    await db.execute(
                        update(SessionEventOutbox).where(SessionEventOutbox.id.in_(
                            statement.order_by(SessionEventOutbox.available_at, SessionEventOutbox.sequence)
                            .limit(max(1, int(limit))).with_for_update(skip_locked=True)
                        )).values(status="publishing", claimed_by=str(worker_id),
                            claim_expires_at=current + timedelta(seconds=self._lease_seconds),
                            attempts=SessionEventOutbox.attempts + 1,
                        ).returning(SessionEventOutbox, select(ChatTranscriptEvent.agent_id).where(
                            ChatTranscriptEvent.id == SessionEventOutbox.event_id).scalar_subquery())
                    )
                ).all()
            )
            claimed: list[ClaimedSessionEvent] = []
            for row, agent_id in sorted(rows, key=lambda pair: (pair[0].available_at, pair[0].sequence)):
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
        envelope_session_id = (
            envelope.get("session_id")
            if envelope.get("schema") == "hive.session_event_compatibility"
            else (envelope.get("scope") or {}).get("session_id")
        )
        if str(envelope_session_id or "") != str(item.session_id):
            raise ValueError("session_event_outbox_session_id_mismatch")
        if int(envelope.get("sequence") or 0) != item.sequence:
            raise ValueError("session_event_outbox_sequence_mismatch")
        if _sha256(envelope) != item.envelope_sha256:
            raise ValueError("session_event_outbox_envelope_hash_mismatch")

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
        published = []
        failures = {}
        for item in items:
            try:
                self._validate_claim(item)
                await publish_callback(item.delivery_payload())
                published.append(item.outbox_id)
            except Exception as exc:  # noqa: BLE001 - durable retry owns transport failures.
                failures[item.outbox_id] = exc
        if items:
            now = datetime.now(UTC)
            async with self._worker_session("ack") as db:
                if published:
                    acknowledged = await db.scalars(update(SessionEventOutbox).where(
                        SessionEventOutbox.id.in_(published), SessionEventOutbox.status == "publishing",
                        SessionEventOutbox.claimed_by == str(worker_id),
                    ).values(status="published", published_at=now, last_error=None,
                        claimed_by=None, claim_expires_at=None).returning(SessionEventOutbox.id))
                    counts["published"] = len(acknowledged.all())
                if failures:
                    rows = await db.scalars(select(SessionEventOutbox).where(
                        SessionEventOutbox.id.in_(failures), SessionEventOutbox.status == "publishing",
                        SessionEventOutbox.claimed_by == str(worker_id),
                    ).with_for_update())
                    for row in rows:
                        error = failures[row.id]
                        row.last_error = f"{type(error).__name__}: {error}"[:4000]
                        row.claimed_by = row.claim_expires_at = None
                        if row.attempts >= self._max_attempts:
                            row.status = "failed"
                            counts["dead_lettered"] += 1
                        else:
                            row.status = "pending"
                            row.available_at = now + timedelta(seconds=self._retry_base_seconds * (2 ** (row.attempts - 1)))
                            counts["retried"] += 1
                await db.commit()
        return counts
