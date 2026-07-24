from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.models.company_knowledge import CompanyKnowledgeEvent, CompanyKnowledgeOutbox
from app.services.company_knowledge_evidence import (
    CompanyKnowledgeEventInput,
    append_company_knowledge_event,
    append_company_knowledge_event_with_outbox,
    verify_company_knowledge_event_chain,
)


class _Result:
    def __init__(self, scalar=None, rows=None) -> None:
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


class _Session:
    def __init__(self, *, previous=None, events=None) -> None:
        self.previous = previous
        self.events = events or []
        self.added: list[object] = []
        self.flush_count = 0
        self.execute_count = 0

    async def execute(self, _statement):
        self.execute_count += 1
        if self.events:
            return _Result(rows=self.events)
        return _Result(scalar=self.previous)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def _event_input(*, tenant_id: uuid.UUID, actor_id: uuid.UUID, user_id: uuid.UUID) -> CompanyKnowledgeEventInput:
    return CompanyKnowledgeEventInput(
        tenant_id=tenant_id,
        event_type="company_knowledge.source_registered",
        actor_type="user",
        actor_id=actor_id,
        accountable_user_id=user_id,
        resource_type="source",
        resource_id=uuid.uuid4(),
        resource_version=1,
        source_refs=("company-source://fixture",),
        source_hash="a" * 64,
        policy_snapshot={"decision": "allow"},
        trace_id="trace-fixture",
        request_id="request-fixture",
        idempotency_key="source-fixture:v1",
        outcome="registered",
        payload={"status": "registered"},
        occurred_at=datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_event_append_hash_chains_without_committing_the_callers_transaction() -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    previous = SimpleNamespace(event_hash="b" * 64)
    session = _Session(previous=previous)

    event = await append_company_knowledge_event(
        session,
        event_input=_event_input(tenant_id=tenant_id, actor_id=user_id, user_id=user_id),
    )

    assert isinstance(event, CompanyKnowledgeEvent)
    assert event.prev_hash == "b" * 64
    assert len(event.event_hash) == 64
    assert session.added == [event]
    assert session.flush_count == 1
    assert not hasattr(session, "commit")


@pytest.mark.asyncio
async def test_event_and_outbox_are_staged_in_one_transaction_with_content_hashes() -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session = _Session()

    event, outbox = await append_company_knowledge_event_with_outbox(
        session,
        event_input=_event_input(tenant_id=tenant_id, actor_id=user_id, user_id=user_id),
        aggregate_type="source",
        aggregate_id=uuid.uuid4(),
        outbox_event_type="company_knowledge.source_registered",
        outbox_idempotency_key="source-fixture:v1:index",
        outbox_payload={"source_id": "fixture", "operation": "index"},
        available_at=datetime(2026, 7, 24, 8, 1, tzinfo=timezone.utc),
    )

    assert isinstance(event, CompanyKnowledgeEvent)
    assert isinstance(outbox, CompanyKnowledgeOutbox)
    assert outbox.event_id == event.id
    assert outbox.tenant_id == event.tenant_id
    assert len(outbox.payload_hash) == 64
    assert session.added == [event, outbox]
    assert session.flush_count == 2


def test_event_chain_verification_reports_the_first_exact_failure() -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    first_input = _event_input(tenant_id=tenant_id, actor_id=user_id, user_id=user_id)
    first = CompanyKnowledgeEvent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        event_type=first_input.event_type,
        actor_type=first_input.actor_type,
        actor_id=first_input.actor_id,
        accountable_user_id=first_input.accountable_user_id,
        resource_type=first_input.resource_type,
        resource_id=first_input.resource_id,
        resource_version=first_input.resource_version,
        source_refs_json=list(first_input.source_refs),
        source_hash=first_input.source_hash,
        policy_snapshot_json=first_input.policy_snapshot,
        trace_id=first_input.trace_id,
        request_id=first_input.request_id,
        idempotency_key=first_input.idempotency_key,
        outcome=first_input.outcome,
        payload_json=first_input.payload,
        prev_hash="",
        event_hash="",
        created_at=first_input.occurred_at,
    )
    from app.services.company_knowledge_evidence import compute_company_knowledge_event_hash

    first.event_hash = compute_company_knowledge_event_hash(first)
    second = CompanyKnowledgeEvent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        event_type="company_knowledge.proposal_created",
        actor_type="user",
        actor_id=user_id,
        accountable_user_id=user_id,
        resource_type="proposal",
        resource_id=uuid.uuid4(),
        resource_version=1,
        source_refs_json=["company-source://fixture"],
        source_hash="c" * 64,
        policy_snapshot_json={"decision": "allow"},
        trace_id="trace-2",
        request_id="request-2",
        idempotency_key="proposal-fixture:v1",
        outcome="created",
        payload_json={"status": "draft"},
        prev_hash=first.event_hash,
        event_hash="",
        created_at=datetime(2026, 7, 24, 8, 2, tzinfo=timezone.utc),
    )
    second.event_hash = compute_company_knowledge_event_hash(second)

    valid = verify_company_knowledge_event_chain([first, second])
    second.payload_json = {"status": "tampered"}
    invalid = verify_company_knowledge_event_chain([first, second])

    assert valid == {"valid": True, "checked": 2, "failed_event_id": None, "reason": None}
    assert invalid == {
        "valid": False,
        "checked": 2,
        "failed_event_id": str(second.id),
        "reason": "event_hash_mismatch",
    }
