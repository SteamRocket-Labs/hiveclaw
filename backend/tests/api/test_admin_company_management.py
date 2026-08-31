from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeDB:
    def __init__(self, results: list[object] | None = None):
        self.sync_session = SimpleNamespace(info={})
        self.statements: list[str] = []
        self.added: list[object] = []
        self._results = list(results or [])
        self.commits = 0

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

    async def commit(self):
        self.commits += 1

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

    def scalar(self):
        return self._value


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


@pytest.mark.parametrize("role", ("org_admin", "member"))
def test_only_platform_admin_can_create_company(role):
    from app.api import admin as admin_api
    from app.core.security import get_current_user
    from app.database import get_db

    db = _FakeDB()
    app = FastAPI()
    app.include_router(admin_api.router)

    async def override_current_user():
        return SimpleNamespace(id=uuid4(), role=role, tenant_id=uuid4())

    async def override_db():
        yield db

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        response = client.post("/admin/companies", json={"name": "Denied Company"})

    assert response.status_code == 403
    assert db.statements == []
    assert db.added == []


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
    assert db.added[1].granted_role == "org_admin"
    assert db.commits == 1
    assert db.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(tenant_id)
    assert any(f"SET LOCAL app.current_tenant_id = '{tenant_id}'" in stmt for stmt in db.statements)
    assert all("BYPASS" not in stmt for stmt in db.statements)


@pytest.mark.asyncio
async def test_platform_admin_company_list_exposes_the_active_org_admin_email():
    from app.api import admin as admin_api

    tenant_id = uuid4()
    tenant = SimpleNamespace(
        id=tenant_id,
        name="Example Owner Lab",
        slug="example-owner-lab",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db = _FakeDB(
        results=[
            _ScalarsResult([tenant]),
            _ScalarResult(3),
            _ScalarResult("owner@example.com"),
            _ScalarResult(2),
            _ScalarResult(1),
            _ScalarResult(123),
        ]
    )

    result = await admin_api.list_companies(
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4()),
        db=db,
    )

    assert len(result) == 1
    assert result[0].org_admin_email == "owner@example.com"


@pytest.mark.asyncio
async def test_platform_admin_toggle_company_pins_target_tenant_before_agent_stop():
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
    assert running_agent.status == "stopped"
    assert any("FOR UPDATE" in stmt for stmt in db.statements)
    assert db.commits == 1
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
