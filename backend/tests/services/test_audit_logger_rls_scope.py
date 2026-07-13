from __future__ import annotations

from contextlib import asynccontextmanager
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

    async def fake_resolve_tenant_for_agent(resolved_agent_id: object, *, session_factory=None) -> object:
        assert resolved_agent_id == agent_id
        assert session_factory is None
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
async def test_write_audit_log_keeps_injected_session_authority_for_resolve_and_insert(monkeypatch):
    from app.services import audit_logger, tenant_resolver

    agent_id = uuid4()
    tenant_id = uuid4()
    injected_factory = object()
    fake_session = _FakeAuditSession()
    observed: dict[str, object] = {}

    async def fake_resolve_tenant_for_agent(resolved_agent_id: object, *, session_factory=None) -> object:
        observed["resolver_agent_id"] = resolved_agent_id
        observed["resolver_factory"] = session_factory
        return tenant_id

    @asynccontextmanager
    async def fake_tenant_scoped_session(resolved_tenant_id: object, *, session_factory=None, **_: object):
        observed["sink_tenant_id"] = resolved_tenant_id
        observed["sink_factory"] = session_factory
        yield fake_session

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(audit_logger, "tenant_scoped_session", fake_tenant_scoped_session)

    await audit_logger.write_audit_log(
        "workflow_run_completed",
        {"run_id": str(uuid4())},
        agent_id=agent_id,
        session_factory=injected_factory,
    )

    assert observed == {
        "resolver_agent_id": agent_id,
        "resolver_factory": injected_factory,
        "sink_tenant_id": tenant_id,
        "sink_factory": injected_factory,
    }
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_agent_audit_with_unresolved_tenant_never_falls_back_to_operator_insert(monkeypatch):
    from app.services import audit_logger, tenant_resolver

    agent_id = uuid4()
    opened_sessions = 0

    async def missing_agent_tenant(resolved_agent_id: object, *, session_factory=None) -> None:
        assert resolved_agent_id == agent_id
        assert session_factory is injected_factory
        return None

    def injected_factory():
        nonlocal opened_sessions
        opened_sessions += 1
        raise AssertionError("unresolved agent audit must not open the operator sink")

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", missing_agent_tenant)

    await audit_logger.write_audit_log(
        "workflow_run_completed",
        agent_id=agent_id,
        session_factory=injected_factory,
    )

    assert opened_sessions == 0
