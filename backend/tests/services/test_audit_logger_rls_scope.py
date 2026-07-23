from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest


class _FakeAuditSession:
    def __init__(self, *, latest_chain_row: dict | None = None, legacy_rows: list[dict] | None = None) -> None:
        self.executed: list[tuple[object, dict[str, object]]] = []
        self.inserted: list[dict[str, object]] = []
        self.committed = False
        self.latest_chain_row = latest_chain_row
        self.legacy_rows = legacy_rows or []

    async def execute(self, statement: object, params: dict[str, object] | None = None):
        resolved_params = params or {}
        self.executed.append((statement, resolved_params))
        statement_text = str(statement)
        if "INSERT INTO audit_logs" in statement_text:
            self.inserted.append(resolved_params)
        if "platform_security_audit.v2" in statement_text and "!=" in statement_text:
            return _MappingResult(self.legacy_rows)
        if "platform_security_audit.v2" in statement_text and "ORDER BY" in statement_text:
            return _MappingResult([self.latest_chain_row] if self.latest_chain_row else [])
        return _MappingResult([])

    async def commit(self) -> None:
        self.committed = True


class _MappingResult:
    def __init__(self, rows: list[dict | None]) -> None:
        self.rows = [row for row in rows if row is not None]

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


@pytest.mark.asyncio
async def test_write_audit_log_uses_tenant_scoped_session_for_agent_rows(monkeypatch):
    from app.services import audit_logger, tenant_resolver

    agent_id = uuid4()
    tenant_id = uuid4()
    seen_tenant_ids: list[object] = []
    fake_session = _FakeAuditSession()

    async def fake_resolve_tenant_for_agent(resolved_agent_id: object) -> object:
        assert resolved_agent_id == agent_id
        return tenant_id

    @asynccontextmanager
    async def fake_tenant_scoped_session(resolved_tenant_id: object, **_: object):
        seen_tenant_ids.append(resolved_tenant_id)
        yield fake_session

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(audit_logger, "tenant_scoped_session", fake_tenant_scoped_session)

    await audit_logger.write_audit_log("heartbeat_fire", {"agent_name": "A"}, agent_id=agent_id)

    assert seen_tenant_ids == [tenant_id]
    assert fake_session.committed is True
    assert fake_session.executed
    assert fake_session.executed[0][1]["tenant_id"] == tenant_id


@pytest.mark.asyncio
async def test_platform_security_audit_uses_operator_bypass_and_preserves_envelope(monkeypatch):
    from app.services import audit_logger

    fake_session = _FakeAuditSession()
    bypass_reasons: list[str] = []

    @asynccontextmanager
    async def fake_async_session():
        yield fake_session

    @asynccontextmanager
    async def fake_enter_rls_bypass(db, *, reason: str):
        assert db is fake_session
        bypass_reasons.append(reason)
        yield db

    monkeypatch.setattr(audit_logger, "async_session", fake_async_session)
    monkeypatch.setattr(audit_logger, "enter_rls_bypass", fake_enter_rls_bypass)

    actor_id = uuid4()
    resource_id = uuid4()
    request_id = "trace-tenant-override"
    event_id = await audit_logger.write_platform_security_audit_event(
        event_type="auth.login_failed",
        severity="warn",
        actor_type="user",
        actor_id=actor_id,
        action="login_failed",
        resource_type="session",
        resource_id=resource_id,
        details={"reason": "invalid_password"},
        ip_address="192.0.2.10",
        request_id=request_id,
        execution_identity_type="delegated_user",
        execution_identity_id=actor_id,
        execution_identity_label="External user via OIDC",
    )

    assert bypass_reasons == ["operator platform security audit insert"]
    assert fake_session.committed is True
    assert len(fake_session.inserted) == 2
    cutover_params, params = fake_session.inserted
    assert cutover_params["action"] == "platform_security.chain_cutover"
    assert event_id == params["id"]
    assert params["action"] == "platform_security.auth.login_failed"
    assert params["tenant_id"] is None
    assert params["agent_id"] is None
    assert params["user_id"] is None
    envelope = json.loads(str(params["details"]))
    assert envelope["schema_version"] == "hive.platform_security_audit.v2"
    assert envelope["event_type"] == "auth.login_failed"
    assert envelope["severity"] == "warn"
    assert envelope["actor"] == {"type": "user", "id": str(actor_id)}
    assert envelope["action"] == "login_failed"
    assert envelope["resource"] == {"type": "session", "id": str(resource_id)}
    assert envelope["details"] == {"reason": "invalid_password"}
    assert envelope["ip_address"] == "192.0.2.10"
    assert envelope["request_id"] == request_id
    assert envelope["execution_identity"] == {
        "type": "delegated_user",
        "id": str(actor_id),
        "label": "External user via OIDC",
    }
    assert envelope["sequence_num"] == 2
    assert envelope["prev_hash"] == json.loads(str(cutover_params["details"]))["event_hash"]
    assert envelope["legacy_anchor"]["legacy_event_count"] == 0
    assert len(envelope["event_hash"]) == 64


@pytest.mark.asyncio
async def test_platform_security_audit_refreshes_legacy_anchor_after_cutover(monkeypatch):
    from app.services import audit_logger
    from app.services.platform_security_audit import seal_platform_security_envelope

    head_id = uuid4()
    head_created_at = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
    empty_anchor = {
        "legacy_event_count": 0,
        "legacy_events_digest": "0" * 64,
        "legacy_first_event_id": None,
        "legacy_last_event_id": None,
    }
    head_envelope = seal_platform_security_envelope(
        event_id=head_id,
        row_action="platform_security.chain_cutover",
        base_envelope={
            "event_type": "chain_cutover",
            "severity": "info",
            "actor": {"type": "system", "id": None},
            "action": "chain_cutover",
            "resource": {"type": "platform_security_audit", "id": None},
            "details": empty_anchor,
            "legacy_anchor": empty_anchor,
            "ip_address": None,
            "request_id": None,
            "execution_identity": None,
        },
        sequence_num=1,
        prev_hash="genesis",
        recorded_at=head_created_at,
    )
    late_legacy_id = uuid4()
    fake_session = _FakeAuditSession(
        latest_chain_row={
            "id": head_id,
            "action": "platform_security.chain_cutover",
            "details": head_envelope,
            "created_at": head_created_at,
        },
        legacy_rows=[
            {
                "id": late_legacy_id,
                "action": "platform_security.auth.login_failed",
                "details": {"schema_version": "hive.platform_security_audit.v1"},
                "created_at": head_created_at,
            }
        ],
    )

    @asynccontextmanager
    async def fake_async_session():
        yield fake_session

    @asynccontextmanager
    async def fake_enter_rls_bypass(db, *, reason: str):
        yield db

    monkeypatch.setattr(audit_logger, "async_session", fake_async_session)
    monkeypatch.setattr(audit_logger, "enter_rls_bypass", fake_enter_rls_bypass)

    await audit_logger.write_platform_security_audit_event(
        event_type="tenant_impersonation",
        severity="warn",
        actor_type="user",
        actor_id=uuid4(),
        action="tenant_impersonation",
    )

    assert len(fake_session.inserted) == 1
    envelope = json.loads(str(fake_session.inserted[0]["details"]))
    assert envelope["sequence_num"] == 2
    assert envelope["prev_hash"] == head_envelope["event_hash"]
    assert envelope["legacy_anchor"]["legacy_event_count"] == 1
    assert envelope["legacy_anchor"]["legacy_first_event_id"] == str(late_legacy_id)


@pytest.mark.asyncio
async def test_platform_security_audit_refuses_to_append_to_tampered_head(monkeypatch):
    from app.services import audit_logger
    from app.services.platform_security_audit import (
        compute_legacy_platform_audit_anchor,
        seal_platform_security_envelope,
    )

    head_id = uuid4()
    head_created_at = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
    anchor = compute_legacy_platform_audit_anchor([])
    head_envelope = seal_platform_security_envelope(
        event_id=head_id,
        row_action="platform_security.chain_cutover",
        base_envelope={
            "event_type": "chain_cutover",
            "severity": "info",
            "actor": {"type": "system", "id": None},
            "action": "chain_cutover",
            "resource": {"type": "platform_security_audit", "id": None},
            "details": anchor,
            "legacy_anchor": anchor,
            "ip_address": None,
            "request_id": None,
            "execution_identity": None,
        },
        sequence_num=1,
        prev_hash="genesis",
        recorded_at=head_created_at,
    )
    head_envelope["severity"] = "tampered"
    fake_session = _FakeAuditSession(
        latest_chain_row={
            "id": head_id,
            "action": "platform_security.chain_cutover",
            "details": head_envelope,
            "created_at": head_created_at,
        }
    )

    @asynccontextmanager
    async def fake_async_session():
        yield fake_session

    @asynccontextmanager
    async def fake_enter_rls_bypass(db, *, reason: str):
        yield db

    monkeypatch.setattr(audit_logger, "async_session", fake_async_session)
    monkeypatch.setattr(audit_logger, "enter_rls_bypass", fake_enter_rls_bypass)

    with pytest.raises(ValueError, match="invalid platform security audit chain head"):
        await audit_logger.write_platform_security_audit_event(
            event_type="tenant_impersonation",
            severity="warn",
            actor_type="user",
            actor_id=uuid4(),
            action="tenant_impersonation",
        )

    assert fake_session.inserted == []


@pytest.mark.asyncio
async def test_platform_security_audit_does_not_swallow_insert_failure(monkeypatch):
    from app.services import audit_logger

    class _FailingSession(_FakeAuditSession):
        async def execute(self, statement: object, params: dict[str, object]) -> None:
            raise RuntimeError("operator audit insert failed")

    failing_session = _FailingSession()

    @asynccontextmanager
    async def fake_async_session():
        yield failing_session

    @asynccontextmanager
    async def fake_enter_rls_bypass(db, *, reason: str):
        yield db

    monkeypatch.setattr(audit_logger, "async_session", fake_async_session)
    monkeypatch.setattr(audit_logger, "enter_rls_bypass", fake_enter_rls_bypass)

    with pytest.raises(RuntimeError, match="operator audit insert failed"):
        await audit_logger.write_platform_security_audit_event(
            event_type="auth.login_failed",
            severity="warn",
            actor_type="user",
            actor_id=uuid4(),
            action="login_failed",
        )


@pytest.mark.asyncio
async def test_best_effort_audit_sink_rejects_platform_security_namespace() -> None:
    from app.services.audit_logger import write_audit_log

    with pytest.raises(ValueError, match="write_platform_security_audit_event"):
        await write_audit_log(
            "platform_security.auth.login_failed",
            details={"reason": "must not bypass the chained sink"},
        )
