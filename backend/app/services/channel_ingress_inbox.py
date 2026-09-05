"""Durable, tenant-governed inbox for authenticated external channel events."""

from __future__ import annotations

import contextlib
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from typing import Any
import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.tenant import Tenant
from app.runtime.tenant_admission import RuntimeTenantPreconditionError
from app.services.channel_secret_storage import (
    CHANNEL_INGRESS_PROVIDER_FIELD,
    CHANNEL_INGRESS_SECRET_PATHS,
)
from app.services.exact_secret_boundary import ExactSecretBoundary

_INGRESS_ID_NAMESPACE = uuid.UUID("cae0a095-76f4-4d84-8515-8d64370f8197")


class ChannelIngressCollisionError(RuntimeError):
    """A provider reused one event identity with a different immutable payload."""


def _uuid(value: Any, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _optional_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    return _uuid(value, field="result id")


def canonical_channel_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Validate JSON portability and return its canonical SHA-256 digest."""

    if not isinstance(payload, dict):
        raise ValueError("channel ingress payload must be an object")
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("channel ingress payload must be JSON serializable") from exc
    return json.loads(encoded), hashlib.sha256(encoded).hexdigest()


def channel_installation_ref(config: Any, *, fallback: str) -> str:
    """Prefer the durable config id; keep legacy/unversioned fixtures deterministic."""

    return str(getattr(config, "id", None) or fallback)


def _channel_ingress_transport_boundary(
    payload: dict[str, Any],
    *,
    provider: str,
) -> ExactSecretBoundary:
    pairs: list[tuple[str, str]] = []
    for path in CHANNEL_INGRESS_SECRET_PATHS.get(
        str(provider or "").strip().casefold(),
        (),
    ):
        value: Any = payload
        for segment in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(segment)
        if isinstance(value, str) and value:
            pairs.append(
                (
                    f"channel-ingress://{provider}/{'/'.join(path)}",
                    value,
                )
            )
    return ExactSecretBoundary.from_pairs(pairs)


def _redact_channel_ingress_failure(
    error: Exception,
    *,
    boundary: ExactSecretBoundary,
) -> str:
    """Preserve useful failure text without persisting exact credential bytes."""

    detail = f"{type(error).__name__}: {error}"
    return boundary.redact_text(detail).text


def _protect_channel_ingress_payload(
    payload: dict[str, Any],
    *,
    provider: str,
    boundary: ExactSecretBoundary,
    phase: str,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    """Redact exact tenant credentials while retaining typed transport secrets."""

    normalized_provider = str(provider or "").strip().casefold()
    canonical_payload, _digest = canonical_channel_payload(payload)
    canonical_payload[CHANNEL_INGRESS_PROVIDER_FIELD] = normalized_provider
    detached: list[tuple[tuple[str, ...], str]] = []
    for path in CHANNEL_INGRESS_SECRET_PATHS.get(normalized_provider, ()):
        parent: Any = canonical_payload
        for segment in path[:-1]:
            if not isinstance(parent, dict):
                parent = None
                break
            parent = parent.get(segment)
        if not isinstance(parent, dict):
            continue
        field_name = path[-1]
        value = parent.get(field_name)
        if isinstance(value, str) and value:
            detached.append((path, value))
            parent[field_name] = None

    redaction = boundary.redact_payload_with_evidence(canonical_payload)
    protected_payload = dict(redaction.value)
    for path, value in detached:
        parent = protected_payload
        for segment in path[:-1]:
            child = parent.get(segment)
            if not isinstance(child, dict):
                child = {}
                parent[segment] = child
            parent = child
        parent[path[-1]] = value

    from app.services.credential_boundary_loader import (
        exact_secret_redaction_receipt,
    )

    return (
        protected_payload,
        exact_secret_redaction_receipt(redaction, phase=phase),
        redaction.redacted_count,
    )


@dataclass(frozen=True, slots=True)
class ChannelIngressSubmission:
    tenant_id: uuid.UUID | str
    agent_id: uuid.UUID | str
    provider: str
    installation_ref: str
    provider_event_id: str
    handler_key: str
    payload: dict[str, Any]
    metadata: dict[str, Any] | None = None
    payload_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelIngressReceipt:
    event_id: uuid.UUID
    created: bool
    payload_digest: str
    status: str


@dataclass(frozen=True, slots=True)
class ClaimedChannelIngressEvent:
    id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    provider: str
    installation_ref: str
    provider_event_id: str
    handler_key: str
    payload_digest: str
    payload: dict[str, Any]
    metadata: dict[str, Any]
    attempt_count: int


def _normalized_submission(submission: ChannelIngressSubmission) -> dict[str, Any]:
    tenant_id = _uuid(submission.tenant_id, field="tenant_id")
    agent_id = _uuid(submission.agent_id, field="agent_id")
    provider = str(submission.provider or "").strip().lower()
    installation_ref = str(submission.installation_ref or "").strip()
    provider_event_id = str(submission.provider_event_id or "").strip()
    handler_key = str(submission.handler_key or "").strip().lower()
    if not all((provider, installation_ref, provider_event_id, handler_key)):
        raise ValueError("channel ingress requires provider, installation, event id, and handler")
    payload, computed_payload_digest = canonical_channel_payload(submission.payload)
    payload[CHANNEL_INGRESS_PROVIDER_FIELD] = provider
    supplied_digest = str(submission.payload_digest or "").strip().lower()
    if supplied_digest and not re.fullmatch(r"[0-9a-f]{64}", supplied_digest):
        raise ValueError("channel ingress payload digest must be a SHA-256 hex digest")
    payload_digest = supplied_digest or computed_payload_digest
    identity = "|".join((str(tenant_id), provider, installation_ref, provider_event_id))
    return {
        "id": uuid.uuid5(_INGRESS_ID_NAMESPACE, identity),
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "provider": provider,
        "installation_ref": installation_ref,
        "provider_event_id": provider_event_id,
        "handler_key": handler_key,
        "payload_digest": payload_digest,
        "payload_json": payload,
        "metadata_json": dict(submission.metadata or {}),
        "status": "received",
        "attempt_count": 0,
        "available_at": datetime.now(UTC),
    }


async def enqueue_channel_ingress_event(
    db: AsyncSession,
    submission: ChannelIngressSubmission,
) -> ChannelIngressReceipt:
    """Insert before provider acknowledgement; exact duplicates are idempotent."""

    values = _normalized_submission(submission)
    stmt = (
        insert(ChannelIngressEvent)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_channel_ingress_provider_event")
        .returning(ChannelIngressEvent.id)
    )
    inserted = (await db.execute(stmt)).scalar_one_or_none()
    if inserted is not None:
        return ChannelIngressReceipt(
            event_id=values["id"],
            created=True,
            payload_digest=values["payload_digest"],
            status="received",
        )

    existing = (
        await db.execute(
            select(ChannelIngressEvent).where(
                ChannelIngressEvent.tenant_id == values["tenant_id"],
                ChannelIngressEvent.provider == values["provider"],
                ChannelIngressEvent.installation_ref == values["installation_ref"],
                ChannelIngressEvent.provider_event_id == values["provider_event_id"],
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        raise RuntimeError("channel ingress conflict row disappeared")
    immutable_matches = (
        existing.agent_id == values["agent_id"]
        and existing.handler_key == values["handler_key"]
        and existing.payload_digest == values["payload_digest"]
    )
    if not immutable_matches:
        raise ChannelIngressCollisionError(
            "provider event identity was reused with a different payload digest or authority"
        )
    return ChannelIngressReceipt(
        event_id=existing.id,
        created=False,
        payload_digest=existing.payload_digest,
        status=existing.status,
    )


async def accept_authenticated_channel_event(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    agent_id: uuid.UUID | str,
    provider: str,
    installation_ref: str,
    provider_event_id: str | None,
    handler_key: str,
    body: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> ChannelIngressReceipt:
    """Commit the durable receipt before the transport emits its acknowledgement."""

    _payload, digest = canonical_channel_payload(body)
    stable_event_id = str(provider_event_id or "").strip() or f"payload:{digest}"
    normalized_provider = str(provider or "").strip().casefold()
    payload_for_redaction = {
        CHANNEL_INGRESS_PROVIDER_FIELD: normalized_provider,
        "body": _payload,
    }

    from app.services.credential_boundary_loader import (
        load_exact_secret_boundary,
    )

    boundary = await load_exact_secret_boundary(
        db,
        tenant_id=_uuid(tenant_id, field="tenant_id"),
        agent_id=_uuid(agent_id, field="agent_id"),
    )
    protected_payload, redaction_receipt, _redacted_count = _protect_channel_ingress_payload(
        payload_for_redaction,
        provider=normalized_provider,
        boundary=boundary,
        phase="channel_inbox",
    )
    protected_metadata = dict(metadata or {})
    if redaction_receipt is not None:
        protected_metadata["exact_secret_ingress_redaction"] = redaction_receipt
    receipt = await enqueue_channel_ingress_event(
        db,
        ChannelIngressSubmission(
            tenant_id=tenant_id,
            agent_id=agent_id,
            provider=provider,
            installation_ref=installation_ref,
            provider_event_id=stable_event_id,
            handler_key=handler_key,
            payload=protected_payload,
            metadata=protected_metadata,
            payload_digest=digest,
        ),
    )
    await db.commit()
    from app.services.channel_ingress_daemon import notify_channel_ingress_daemon

    notify_channel_ingress_daemon()
    return receipt


async def migrate_channel_ingress_exact_secret_rows(
    db: AsyncSession,
    *,
    apply: bool,
) -> dict[str, Any]:
    """Dry-run or redact currently bound exact credentials in legacy inbox rows."""

    from app.services.credential_boundary_loader import load_exact_secret_boundary

    rows = list(
        (
            await db.execute(
                select(ChannelIngressEvent).order_by(
                    ChannelIngressEvent.tenant_id,
                    ChannelIngressEvent.agent_id,
                    ChannelIngressEvent.received_at,
                    ChannelIngressEvent.id,
                )
            )
        )
        .scalars()
        .all()
    )
    boundaries: dict[tuple[uuid.UUID, uuid.UUID], ExactSecretBoundary] = {}
    rows_requiring_redaction = 0
    redacted_values = 0
    rows_rewritten = 0
    for row in rows:
        key = (row.tenant_id, row.agent_id)
        boundary = boundaries.get(key)
        if boundary is None:
            boundary = await load_exact_secret_boundary(
                db,
                tenant_id=row.tenant_id,
                agent_id=row.agent_id,
            )
            boundaries[key] = boundary
        protected, receipt, count = _protect_channel_ingress_payload(
            dict(row.payload_json or {}),
            provider=row.provider,
            boundary=boundary,
            phase="channel_inbox_legacy_backfill",
        )
        if count <= 0:
            continue
        rows_requiring_redaction += 1
        redacted_values += count
        if not apply:
            continue
        row.payload_json = protected
        metadata = dict(row.metadata_json or {})
        if receipt is not None:
            metadata["exact_secret_ingress_redaction"] = receipt
        row.metadata_json = metadata
        rows_rewritten += 1

    if apply:
        await db.commit()
    return {
        "schema": "hive.channel_ingress_exact_secret_backfill.v1",
        "mode": "apply" if apply else "dry_run",
        "rows_scanned": len(rows),
        "rows_requiring_redaction": rows_requiring_redaction,
        "redacted_values": redacted_values,
        "rows_rewritten": rows_rewritten,
    }


async def wait_for_channel_ingress_result(
    *,
    tenant_id: uuid.UUID | str,
    event_id: uuid.UUID | str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    timeout_seconds: float = 120,
    poll_seconds: float = 0.2,
) -> dict[str, Any]:
    """Wait without owning execution; stream transports use this only to emit their reply."""

    tenant_uuid = _uuid(tenant_id, field="tenant_id")
    event_uuid = _uuid(event_id, field="event_id")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, float(timeout_seconds))
    factory = session_factory or async_session
    while True:
        async with tenant_scoped_session(
            tenant_uuid,
            session_factory=factory,
            require_tenant=True,
            source="wait_for_channel_ingress_result",
        ) as db:
            row = (
                await db.execute(
                    select(ChannelIngressEvent).where(
                        ChannelIngressEvent.id == event_uuid,
                        ChannelIngressEvent.tenant_id == tenant_uuid,
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            raise LookupError("channel ingress event not found")
        if row.status == "processed":
            return dict(row.processing_receipt_json or {})
        if row.status == "dead_letter":
            raise RuntimeError(row.last_error or "channel ingress event reached dead letter")
        if loop.time() >= deadline:
            raise TimeoutError("channel ingress processing did not finish before transport timeout")
        await asyncio.sleep(max(0.01, float(poll_seconds)))


def _claimed(row: ChannelIngressEvent) -> ClaimedChannelIngressEvent:
    return ClaimedChannelIngressEvent(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        provider=row.provider,
        installation_ref=row.installation_ref,
        provider_event_id=row.provider_event_id,
        handler_key=row.handler_key,
        payload_digest=row.payload_digest,
        payload=dict(row.payload_json or {}),
        metadata=dict(row.metadata_json or {}),
        attempt_count=int(row.attempt_count or 0),
    )


class ChannelIngressInboxService:
    """Cross-worker claim, dispatch, retry and terminal acknowledgement."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        lease_seconds: int = 300,
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
            async with enter_rls_bypass(db, reason=f"channel_ingress_inbox.{operation}") as bypass_db:
                yield bypass_db

    async def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[ClaimedChannelIngressEvent]:
        current = now or datetime.now(UTC)
        expired_lock = current - timedelta(seconds=self._lease_seconds)
        async with self._worker_session("claim") as db:
            rows = list(
                (
                    await db.execute(
                        select(ChannelIngressEvent)
                        .where(
                            or_(
                                and_(
                                    ChannelIngressEvent.status.in_(("received", "failed")),
                                    ChannelIngressEvent.available_at <= current,
                                ),
                                and_(
                                    ChannelIngressEvent.status == "processing",
                                    ChannelIngressEvent.locked_at <= expired_lock,
                                ),
                            ),
                            # Provider ingress is authenticated only by the
                            # channel identity; tenant liveness is re-checked
                            # at claim time so a company deactivated by the
                            # admin toggle or tenant deletion cannot dispatch
                            # queued channel events into its Agent runtime.
                            # Events stay durable and resume on reactivation.
                            ChannelIngressEvent.tenant_id.in_(select(Tenant.id).where(Tenant.is_active.is_(True))),
                        )
                        .order_by(ChannelIngressEvent.available_at, ChannelIngressEvent.received_at)
                        .limit(max(1, int(limit)))
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.status = "processing"
                row.locked_by = str(worker_id)
                row.locked_at = current
                row.attempt_count = int(row.attempt_count or 0) + 1
            await db.commit()
            return [_claimed(row) for row in rows]

    async def _mark_processed(
        self,
        *,
        item: ClaimedChannelIngressEvent,
        worker_id: str,
        receipt: dict[str, Any],
    ) -> bool:
        async with self._worker_session("ack") as db:
            row = (
                await db.execute(select(ChannelIngressEvent).where(ChannelIngressEvent.id == item.id).with_for_update())
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                return False
            row.status = "processed"
            row.processed_at = datetime.now(UTC)
            row.processing_receipt_json = dict(receipt)
            row.result_runtime_task_id = _optional_uuid(receipt.get("runtime_task_id"))
            row.result_session_id = _optional_uuid(receipt.get("session_id"))
            row.external_principal_id = _optional_uuid(receipt.get("external_principal_id"))
            row.last_error = None
            row.locked_by = None
            row.locked_at = None
            await db.commit()
            return True

    async def _mark_failed(
        self,
        *,
        item: ClaimedChannelIngressEvent,
        worker_id: str,
        error: Exception,
    ) -> str:
        now = datetime.now(UTC)
        async with self._worker_session("retry") as db:
            row = (
                await db.execute(select(ChannelIngressEvent).where(ChannelIngressEvent.id == item.id).with_for_update())
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                return "stale"
            try:
                from app.services.credential_boundary_loader import (
                    load_exact_secret_boundary,
                )

                tenant_boundary = await load_exact_secret_boundary(
                    db,
                    tenant_id=item.tenant_id,
                    agent_id=item.agent_id,
                )
                boundary = ExactSecretBoundary.combine(
                    tenant_boundary,
                    _channel_ingress_transport_boundary(
                        item.payload,
                        provider=item.provider,
                    ),
                )
                row.last_error = _redact_channel_ingress_failure(
                    error,
                    boundary=boundary,
                )
            except Exception:
                # Failure handling must never persist the unsanitized exception
                # merely because the credential authority is unavailable.
                row.last_error = f"{type(error).__name__}: credential_boundary_unavailable"
            row.locked_by = None
            row.locked_at = None
            if int(row.attempt_count or 0) >= self._max_attempts:
                row.status = "dead_letter"
                outcome = "dead_letter"
            else:
                delay = min(300, self._retry_base_seconds * (2 ** max(0, int(row.attempt_count or 1) - 1)))
                row.status = "failed"
                row.available_at = now + timedelta(seconds=delay)
                outcome = "retry"
            await db.commit()
            return outcome

    async def _release_for_inactive_tenant(
        self,
        *,
        item: ClaimedChannelIngressEvent,
        worker_id: str,
    ) -> bool:
        """Release a claim whose tenant was deactivated after the claim.

        A company deactivation is not a failure of the event: the row returns
        to ``received`` with its pre-claim attempt budget restored, so
        reactivation can process it. The deferral never marks the row
        processed, consumes its finite failure attempts, or dead-letters it.
        The row keeps the ``available_at`` it was already eligible under at
        claim time: rewriting it would let events that arrived after this one
        overtake it once the company is reactivated.
        """

        async with self._worker_session("defer") as db:
            row = (
                await db.execute(select(ChannelIngressEvent).where(ChannelIngressEvent.id == item.id).with_for_update())
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                return False
            row.status = "received"
            row.attempt_count = max(0, int(row.attempt_count or 0) - 1)
            row.locked_by = None
            row.locked_at = None
            await db.commit()
            return True

    async def drain_once(
        self,
        *,
        worker_id: str,
        dispatch: Callable[[ClaimedChannelIngressEvent], Awaitable[dict[str, Any]]] | None = None,
        limit: int = 20,
    ) -> dict[str, int]:
        if dispatch is None:
            from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event

            dispatch = dispatch_channel_ingress_event
        stats = {"claimed": 0, "processed": 0, "retried": 0, "dead_lettered": 0, "deferred": 0}
        items = await self.claim_batch(worker_id=worker_id, limit=limit)
        stats["claimed"] = len(items)
        for item in items:
            try:
                receipt = await dispatch(item)
                if await self._mark_processed(item=item, worker_id=worker_id, receipt=receipt):
                    stats["processed"] += 1
            except Exception as exc:
                if isinstance(exc, RuntimeTenantPreconditionError) and exc.reason_code == "tenant_inactive":
                    # The tenant was deactivated after this claim was committed;
                    # defer the durable row instead of counting a failure.
                    if await self._release_for_inactive_tenant(item=item, worker_id=worker_id):
                        stats["deferred"] += 1
                    continue
                outcome = await self._mark_failed(item=item, worker_id=worker_id, error=exc)
                if outcome == "retry":
                    stats["retried"] += 1
                elif outcome == "dead_letter":
                    stats["dead_lettered"] += 1
        return stats


__all__ = [
    "ChannelIngressCollisionError",
    "ChannelIngressInboxService",
    "ChannelIngressReceipt",
    "ChannelIngressSubmission",
    "ClaimedChannelIngressEvent",
    "canonical_channel_payload",
    "channel_installation_ref",
    "accept_authenticated_channel_event",
    "enqueue_channel_ingress_event",
    "wait_for_channel_ingress_result",
]
