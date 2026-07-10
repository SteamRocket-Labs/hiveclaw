from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeDB:
    def __init__(self, results: list[object] | None = None):
        self.sync_session = SimpleNamespace(info={})
        self.statements: list[str] = []
        self.added: list[object] = []
        self._results = list(results or [])

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid4()
            if hasattr(value, "is_active") and getattr(value, "is_active", None) is None:
                value.is_active = True
            if hasattr(value, "created_at") and getattr(value, "created_at", None) is None:
                value.created_at = datetime.now(timezone.utc)

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        if "SET LOCAL" in str(stmt):
            return SimpleNamespace()
        if self._results:
            return self._results.pop(0)
        return SimpleNamespace()


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


@pytest.mark.asyncio
async def test_platform_admin_create_company_pins_new_tenant_before_invite_insert():
    from app import database
    from app.api import admin as admin_api

    db = _FakeDB()
    result = await admin_api.create_company(
        data=admin_api.CompanyCreateRequest(name="RLS Target Company"),
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4()),
        db=db,
    )

    tenant_id = result.company.id
    assert db.added[1].tenant_id == tenant_id
    assert db.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(tenant_id)
    assert any(f"SET LOCAL app.current_tenant_id = '{tenant_id}'" in stmt for stmt in db.statements)


@pytest.mark.asyncio
async def test_platform_admin_toggle_company_pins_target_tenant_before_agent_pause():
    from app import database
    from app.api import admin as admin_api

    company_id = uuid4()
    tenant = SimpleNamespace(id=company_id, is_active=True)
    running_agent = SimpleNamespace(id=uuid4(), tenant_id=company_id, status="running")
    db = _FakeDB(results=[_ScalarResult(tenant), _ScalarsResult([running_agent])])

    result = await admin_api.toggle_company(
        company_id=company_id,
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4()),
        db=db,
    )

    assert result == {"ok": True, "is_active": False}
    assert tenant.is_active is False
    assert running_agent.status == "paused"
    assert db.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(company_id)
    assert any(f"SET LOCAL app.current_tenant_id = '{company_id}'" in stmt for stmt in db.statements)


@pytest.mark.asyncio
async def test_platform_admin_backfill_prose_endpoint_is_retired(monkeypatch):
    """C7 cutover: the flat-T3 prose backfill admin endpoint reports retirement."""
    from app.api import admin as admin_api

    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4(), name="Target Agent")
    db = _FakeDB(results=[_ScalarResult(agent)])

    result = await admin_api.backfill_agent_prose(
        agent_id=agent_id,
        dry_run=True,
        _admin=SimpleNamespace(id=uuid4(), is_platform_admin=True),
        db=db,
    )

    assert result["status"] == "retired"
