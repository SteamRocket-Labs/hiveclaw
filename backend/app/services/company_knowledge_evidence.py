"""Append-only Company Knowledge event chain and transactional outbox helpers."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.models.company_knowledge import CompanyKnowledgeEvent, CompanyKnowledgeOutbox
from app.models.tenant import Tenant


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeEventInput:
    tenant_id: uuid.UUID
    event_type: str
    actor_type: str
    actor_id: uuid.UUID
    accountable_user_id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID | None
    resource_version: int | None
    source_refs: tuple[str, ...]
    source_hash: str | None
    policy_snapshot: dict[str, Any]
    trace_id: str
    request_id: str | None
    idempotency_key: str
    outcome: str
    payload: dict[str, Any]
    occurred_at: datetime


def _event_hash_payload(event: CompanyKnowledgeEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "tenant_id": event.tenant_id,
        "event_type": event.event_type,
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "accountable_user_id": event.accountable_user_id,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "resource_version": event.resource_version,
        "source_refs": list(event.source_refs_json or []),
        "source_hash": event.source_hash,
        "policy_snapshot": dict(event.policy_snapshot_json or {}),
        "trace_id": event.trace_id,
        "request_id": event.request_id,
        "idempotency_key": event.idempotency_key,
        "outcome": event.outcome,
        "payload": dict(event.payload_json or {}),
        "prev_hash": event.prev_hash,
        "created_at": event.created_at,
    }


def compute_company_knowledge_event_hash(event: CompanyKnowledgeEvent) -> str:
    return _sha256(_event_hash_payload(event))


def _event_matches_input(event: CompanyKnowledgeEvent, event_input: CompanyKnowledgeEventInput) -> bool:
    return _event_hash_payload(event) == {
        "id": event.id,
        "tenant_id": event_input.tenant_id,
        "event_type": event_input.event_type,
        "actor_type": event_input.actor_type,
        "actor_id": event_input.actor_id,
        "accountable_user_id": event_input.accountable_user_id,
        "resource_type": event_input.resource_type,
        "resource_id": event_input.resource_id,
        "resource_version": event_input.resource_version,
        "source_refs": list(event_input.source_refs),
        "source_hash": event_input.source_hash,
        "policy_snapshot": dict(event_input.policy_snapshot),
        "trace_id": event_input.trace_id,
        "request_id": event_input.request_id,
        "idempotency_key": event_input.idempotency_key,
        "outcome": event_input.outcome,
        "payload": dict(event_input.payload),
        "prev_hash": event.prev_hash,
        "created_at": event_input.occurred_at,
    }


async def append_company_knowledge_event(
    session: Any,
    *,
    event_input: CompanyKnowledgeEventInput,
) -> CompanyKnowledgeEvent:
    """Stage one chained event in the caller's transaction; never commit here."""

    # The tenant row is the stable per-stream mutex, including the first event
    # where no prior event row exists to lock.
    await session.execute(select(Tenant.id).where(Tenant.id == event_input.tenant_id).with_for_update())
    previous = (
        await session.execute(
            select(CompanyKnowledgeEvent)
            .where(CompanyKnowledgeEvent.tenant_id == event_input.tenant_id)
            .order_by(
                (CompanyKnowledgeEvent.idempotency_key == event_input.idempotency_key).desc(),
                CompanyKnowledgeEvent.stream_sequence.desc(),
            )
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if previous is not None and getattr(previous, "idempotency_key", None) == event_input.idempotency_key:
        if not _event_matches_input(previous, event_input):
            raise ValueError("company_knowledge_event_idempotency_conflict")
        return previous

    event = CompanyKnowledgeEvent(
        id=uuid.uuid4(),
        tenant_id=event_input.tenant_id,
        event_type=event_input.event_type,
        actor_type=event_input.actor_type,
        actor_id=event_input.actor_id,
        accountable_user_id=event_input.accountable_user_id,
        resource_type=event_input.resource_type,
        resource_id=event_input.resource_id,
        resource_version=event_input.resource_version,
        source_refs_json=list(event_input.source_refs),
        source_hash=event_input.source_hash,
        policy_snapshot_json=dict(event_input.policy_snapshot),
        trace_id=event_input.trace_id,
        request_id=event_input.request_id,
        idempotency_key=event_input.idempotency_key,
        outcome=event_input.outcome,
        payload_json=dict(event_input.payload),
        stream_sequence=int(getattr(previous, "stream_sequence", 0) or 0) + 1,
        prev_hash=str(getattr(previous, "event_hash", "") or ""),
        event_hash="",
        created_at=event_input.occurred_at,
    )
    event.event_hash = compute_company_knowledge_event_hash(event)
    session.add(event)
    await session.flush()
    return event


async def append_company_knowledge_event_with_outbox(
    session: Any,
    *,
    event_input: CompanyKnowledgeEventInput,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    outbox_event_type: str,
    outbox_idempotency_key: str,
    outbox_payload: dict[str, Any],
    available_at: datetime,
) -> tuple[CompanyKnowledgeEvent, CompanyKnowledgeOutbox]:
    """Stage event and projection work atomically in the caller's transaction."""

    event = await append_company_knowledge_event(session, event_input=event_input)
    existing = (
        await session.execute(
            select(CompanyKnowledgeOutbox)
            .where(
                CompanyKnowledgeOutbox.tenant_id == event_input.tenant_id,
                CompanyKnowledgeOutbox.idempotency_key == outbox_idempotency_key,
            )
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    payload_hash = _sha256(outbox_payload)
    if existing is not None and getattr(existing, "idempotency_key", None) == outbox_idempotency_key:
        if (
            getattr(existing, "event_id", None) != event.id
            or getattr(existing, "aggregate_id", None) != aggregate_id
            or getattr(existing, "payload_hash", None) != payload_hash
        ):
            raise ValueError("company_knowledge_outbox_idempotency_conflict")
        return event, existing

    outbox = CompanyKnowledgeOutbox(
        id=uuid.uuid4(),
        tenant_id=event_input.tenant_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_id=event.id,
        event_type=outbox_event_type,
        idempotency_key=outbox_idempotency_key,
        payload_json=dict(outbox_payload),
        payload_hash=payload_hash,
        status="pending",
        available_at=available_at,
        attempt_count=0,
        max_attempts=8,
    )
    session.add(outbox)
    await session.flush()
    return event, outbox


def verify_company_knowledge_event_chain(events: list[CompanyKnowledgeEvent]) -> dict[str, Any]:
    expected_prev_hash = ""
    expected_sequence = 1
    checked = 0
    for event in events:
        checked += 1
        if event.stream_sequence != expected_sequence:
            return {
                "valid": False,
                "checked": checked,
                "failed_event_id": str(event.id),
                "reason": "stream_sequence_mismatch",
            }
        if event.prev_hash != expected_prev_hash:
            return {
                "valid": False,
                "checked": checked,
                "failed_event_id": str(event.id),
                "reason": "previous_hash_mismatch",
            }
        if event.event_hash != compute_company_knowledge_event_hash(event):
            return {
                "valid": False,
                "checked": checked,
                "failed_event_id": str(event.id),
                "reason": "event_hash_mismatch",
            }
        expected_prev_hash = event.event_hash
        expected_sequence += 1
    return {"valid": True, "checked": checked, "failed_event_id": None, "reason": None}


__all__ = [
    "CompanyKnowledgeEventInput",
    "append_company_knowledge_event",
    "append_company_knowledge_event_with_outbox",
    "compute_company_knowledge_event_hash",
    "verify_company_knowledge_event_chain",
]
