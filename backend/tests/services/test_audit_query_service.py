from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _FakeAuditQueryDB:
    def __init__(self, *values: object) -> None:
        self._values = list(values)
        self.executed: list[object] = []

    async def execute(self, statement):
        self.executed.append(statement)
        return _ScalarResult(self._values.pop(0) if self._values else None)


def _legacy_audit_hash(event) -> str:
    payload = json.dumps(
        {
            "event_type": event.event_type,
            "actor_type": event.actor_type,
            "actor_id": str(event.actor_id),
            "tenant_id": str(event.tenant_id),
            "action": event.action,
            "prev_hash": event.prev_hash,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@pytest.mark.asyncio
async def test_verify_chain_accepts_legacy_pre_details_hashes() -> None:
    from app.services.audit_query_service import verify_chain

    tenant_id = uuid4()
    event = SimpleNamespace(
        id=uuid4(),
        event_type="tool.execution",
        severity="info",
        actor_type="agent",
        actor_id=uuid4(),
        tenant_id=tenant_id,
        action="read_file",
        resource_type="tool",
        resource_id=uuid4(),
        details={"path": "workspace/notes.md"},
        ip_address=None,
        request_id=None,
        prev_hash="genesis",
    )
    event.event_hash = _legacy_audit_hash(event)
    db = _FakeAuditQueryDB(event)

    result = await verify_chain(db, event.id, tenant_id)  # type: ignore[arg-type]

    assert result["valid"] is True
    assert result["hash_version"] == "legacy_v1"
    assert result["computed_hash"] == event.event_hash
