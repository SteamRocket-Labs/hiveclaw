from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
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


class _AuditListResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)


class _FakeAuditExportDB:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    async def execute(self, _statement):
        return _AuditListResult(self._values)


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


def test_admin_audit_search_never_queries_raw_details() -> None:
    from sqlalchemy import select

    from app.models.security_audit import SecurityAuditEvent
    from app.schemas.audit_schemas import AuditQueryParams
    from app.services.audit_query_service import _apply_filters

    query = _apply_filters(
        select(SecurityAuditEvent.id),
        uuid4(),
        AuditQueryParams(search="customer-private-marker"),
    )

    where_sql = str(query.whereclause)
    assert "security_audit_events.action" in where_sql
    assert "security_audit_events.event_type" in where_sql
    assert "security_audit_events.details" not in where_sql


@pytest.mark.asyncio
async def test_admin_audit_csv_exports_only_summary_details() -> None:
    from app.schemas.audit_schemas import AuditQueryParams
    from app.services.audit_query_service import export_csv

    event = SimpleNamespace(
        created_at=datetime.now(timezone.utc),
        event_type="llm_model.test_completed",
        severity="info",
        actor_type="user",
        actor_id=uuid4(),
        action="test_llm_model_completed",
        resource_type="llm_model",
        resource_id=uuid4(),
        ip_address="203.0.113.10",
        details={
            "provider": "zhipu",
            "model": "glm-5.3",
            "success": True,
            "probe_id": "probe-safe",
            "session_id": "session-private",
            "reason": "customer recovery note",
            "api_key": "secret-key",
        },
    )

    result = await export_csv(
        _FakeAuditExportDB([event]),  # type: ignore[arg-type]
        uuid4(),
        AuditQueryParams(),
    )

    assert "provider" in result
    assert "probe-safe" in result
    assert "session-private" not in result
    assert "customer recovery note" not in result
    assert "secret-key" not in result
    assert "203.0.113.10" not in result
    assert "ip_address" not in result.splitlines()[0]


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


@pytest.mark.asyncio
async def test_verify_chain_accepts_identity_aware_canonical_hashes() -> None:
    from app.core.policy import compute_audit_event_hash
    from app.services.audit_query_service import verify_chain

    tenant_id = uuid4()
    delegated_user_id = uuid4()
    event = SimpleNamespace(
        id=uuid4(),
        event_type="tool.execution",
        severity="info",
        actor_type="agent",
        actor_id=uuid4(),
        tenant_id=tenant_id,
        action="send_email",
        resource_type="tool",
        resource_id=uuid4(),
        details={"recipient": "customer@example.com"},
        ip_address=None,
        request_id=uuid4(),
        prev_hash="genesis",
        execution_identity_type="delegated_user",
        execution_identity_id=delegated_user_id,
        execution_identity_label="Example Owner via web",
    )
    event.event_hash = compute_audit_event_hash(
        event_type=event.event_type,
        severity=event.severity,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        tenant_id=event.tenant_id,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        details=event.details,
        ip_address=event.ip_address,
        request_id=event.request_id,
        prev_hash=event.prev_hash,
        execution_identity_type=event.execution_identity_type,
        execution_identity_id=event.execution_identity_id,
        execution_identity_label=event.execution_identity_label,
    )
    db = _FakeAuditQueryDB(event)

    result = await verify_chain(db, event.id, tenant_id)  # type: ignore[arg-type]

    assert result["valid"] is True
    assert result["hash_version"] == "canonical_v3"
    assert result["computed_hash"] == event.event_hash


@pytest.mark.asyncio
async def test_verify_chain_accepts_pre_identity_canonical_hashes_with_identity_columns() -> None:
    from app.core.policy import compute_audit_event_hash
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
        execution_identity_type="delegated_user",
        execution_identity_id=uuid4(),
        execution_identity_label="Example Owner via web",
    )
    event.event_hash = compute_audit_event_hash(
        event_type=event.event_type,
        severity=event.severity,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        tenant_id=event.tenant_id,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        details=event.details,
        ip_address=event.ip_address,
        request_id=event.request_id,
        prev_hash=event.prev_hash,
    )
    db = _FakeAuditQueryDB(event)

    result = await verify_chain(db, event.id, tenant_id)  # type: ignore[arg-type]

    assert result["valid"] is True
    assert result["hash_version"] == "canonical_v2"
    assert result["computed_hash"] == event.event_hash
