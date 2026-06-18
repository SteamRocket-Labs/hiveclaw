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
