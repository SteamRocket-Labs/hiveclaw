from __future__ import annotations

from contextlib import asynccontextmanager
import json
from uuid import uuid4

import pytest


class _FakeAuditSession:
    def __init__(self) -> None:
        self.executed: list[tuple[object, dict[str, object]]] = []
        self.committed = False

    async def execute(self, statement: object, params: dict[str, object]) -> None:
        self.executed.append((statement, params))

    async def commit(self) -> None:
        self.committed = True


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
    assert len(fake_session.executed) == 1
    params = fake_session.executed[0][1]
    assert event_id == params["id"]
    assert params["action"] == "platform_security.auth.login_failed"
    assert params["tenant_id"] is None
    assert params["agent_id"] is None
    assert params["user_id"] is None
    envelope = json.loads(str(params["details"]))
    assert envelope == {
        "schema_version": "hive.platform_security_audit.v1",
        "event_type": "auth.login_failed",
        "severity": "warn",
        "actor": {"type": "user", "id": str(actor_id)},
        "action": "login_failed",
        "resource": {"type": "session", "id": str(resource_id)},
        "details": {"reason": "invalid_password"},
        "ip_address": "192.0.2.10",
        "request_id": request_id,
        "execution_identity": {
            "type": "delegated_user",
            "id": str(actor_id),
            "label": "External user via OIDC",
        },
    }


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
