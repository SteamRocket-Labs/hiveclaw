from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.tenants as tenants_api


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        values = self._value if isinstance(self._value, list) else [self._value]
        return SimpleNamespace(all=lambda: values)


class _FakeDB:
    def __init__(self, tenant):
        self.tenant = tenant
        self.flushed = False
        self.statements = []
        self.statement_params = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        self.statement_params.append(stmt.compile().params)
        return _ScalarResult(self.tenant)

    async def flush(self):
        self.flushed = True


class _SequenceDB:
    def __init__(self, results):
        self._results = list(results)
        self.flushed = False
        self.added = []

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    async def flush(self):
        self.flushed = True

    def add(self, value):
        self.added.append(value)


def _build_client(*, current_user, tenant):
    app = FastAPI()
    app.include_router(tenants_api.router)
    fake_db = _FakeDB(tenant)

    async def override_current_user():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[tenants_api.get_current_user] = override_current_user
    app.dependency_overrides[tenants_api.get_db] = override_db
    return TestClient(app), fake_db


def _tenant(tenant_id):
    return SimpleNamespace(
        id=tenant_id,
        name="Acme",
        slug="acme",
        im_provider="web_only",
        timezone="UTC",
        is_active=True,
        default_tokens_per_day=1000,
        default_tokens_per_month=20000,
        created_at=None,
    )


def test_org_admin_can_update_own_tenant_name_and_timezone():
    tenant_id = uuid4()
    client, fake_db = _build_client(
        current_user=SimpleNamespace(role="org_admin", tenant_id=tenant_id),
        tenant=_tenant(tenant_id),
    )

    response = client.put(
        f"/tenants/{tenant_id}",
        json={"name": "Acme CN", "timezone": "Asia/Shanghai"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Acme CN"
    assert response.json()["timezone"] == "Asia/Shanghai"
    assert fake_db.flushed is True


def test_org_admin_cannot_update_other_tenant():
    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    client, _ = _build_client(
        current_user=SimpleNamespace(role="org_admin", tenant_id=own_tenant_id),
        tenant=_tenant(target_tenant_id),
    )

    response = client.put(
        f"/tenants/{target_tenant_id}",
        json={"name": "Other Co"},
    )

    assert response.status_code == 403


def test_org_admin_cannot_update_restricted_tenant_fields():
    tenant_id = uuid4()
    client, _ = _build_client(
        current_user=SimpleNamespace(role="org_admin", tenant_id=tenant_id),
        tenant=_tenant(tenant_id),
    )

    response = client.put(
        f"/tenants/{tenant_id}",
        json={"is_active": False},
    )

    assert response.status_code == 403


def test_platform_admin_list_tenants_uses_audited_bypass(monkeypatch):
    tenant_id = uuid4()
    fake_db = _FakeDB([_tenant(tenant_id)])
    bypass_calls: list[dict] = []
    current_user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4())

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        bypass_calls.append({"session": session, "reason": reason, "actor_id": actor_id})
        yield session

    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass, raising=False)

    result = tenants_api.list_tenants(
        current_user=current_user,
        db=fake_db,
    )
    tenants = __import__("asyncio").run(result)

    assert [tenant.id for tenant in tenants] == [tenant_id]
    assert "WHERE tenants.id !=" in fake_db.statements[0]
    assert str(next(iter(fake_db.statement_params[0].values()))) == "00000000-0000-4000-8000-000000000023"
    assert bypass_calls == [
        {
            "session": fake_db,
            "reason": "platform-admin list tenants",
            "actor_id": str(current_user.id),
        }
    ]


def test_platform_admin_update_other_tenant_uses_audited_bypass(monkeypatch):
    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4())
    fake_db = _FakeDB(_tenant(tenant_id))
    bypass_calls: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        bypass_calls.append({"session": session, "reason": reason, "actor_id": actor_id})
        yield session

    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass, raising=False)

    result = tenants_api.update_tenant(
        tenant_id=tenant_id,
        data=tenants_api.TenantUpdate(name="Renamed"),
        current_user=current_user,
        db=fake_db,
    )
    tenant = __import__("asyncio").run(result)

    assert tenant.name == "Renamed"
    assert fake_db.flushed is True
    assert bypass_calls == [
        {
            "session": fake_db,
            "reason": "platform-admin update tenant",
            "actor_id": str(current_user.id),
        }
    ]


def test_platform_admin_assign_user_to_tenant_uses_audited_bypass(monkeypatch):
    tenant_id = uuid4()
    user_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4())
    target_user = SimpleNamespace(
        id=user_id,
        tenant_id=None,
        role="member",
        department_id=uuid4(),
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
    )
    fake_db = _SequenceDB([_ScalarResult(_tenant(tenant_id)), _ScalarResult(target_user)])
    bypass_calls: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        bypass_calls.append({"session": session, "reason": reason, "actor_id": actor_id})
        yield session

    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass, raising=False)

    result = tenants_api.assign_user_to_tenant(
        tenant_id=tenant_id,
        user_id=user_id,
        role="org_admin",
        current_user=current_user,
        db=fake_db,
    )
    payload = __import__("asyncio").run(result)

    assert payload["status"] == "ok"
    assert target_user.tenant_id == tenant_id
    assert target_user.role == "org_admin"
    assert target_user.department_id is None
    assert target_user.quota_tokens_per_day == 1000
    assert target_user.quota_tokens_per_month == 20000
    assert len(fake_db.added) == 1
    assert fake_db.added[0].action == "tenant:user_assigned"
    assert fake_db.added[0].details == {
        "target_user_id": str(user_id),
        "previous_tenant_id": None,
        "previous_role": "member",
        "role": "org_admin",
    }
    assert fake_db.flushed is True
    assert bypass_calls == [
        {
            "session": fake_db,
            "reason": "platform-admin assign user to tenant",
            "actor_id": str(current_user.id),
        }
    ]


def test_platform_admin_assign_user_by_email_uses_same_tenantless_gate(monkeypatch):
    tenant_id = uuid4()
    user_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4())
    target_user = SimpleNamespace(
        id=user_id,
        email="new-admin@example.com",
        tenant_id=None,
        role="member",
        department_id=None,
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
    )
    fake_db = _SequenceDB([_ScalarResult(_tenant(tenant_id)), _ScalarResult(target_user)])

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        yield session

    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass, raising=False)

    result = tenants_api.assign_user_to_tenant_by_email(
        tenant_id=tenant_id,
        data=tenants_api.TenantUserAssignment(email="NEW-ADMIN@example.com", role="org_admin"),
        current_user=current_user,
        db=fake_db,
    )
    payload = __import__("asyncio").run(result)

    assert payload == {
        "status": "ok",
        "user_id": str(user_id),
        "tenant_id": str(tenant_id),
        "role": "org_admin",
        "reauthentication_required": True,
    }


def test_platform_admin_assign_user_rejects_cross_tenant_move(monkeypatch):
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4())
    target_user = SimpleNamespace(id=uuid4(), tenant_id=other_tenant_id, role="member")
    fake_db = _SequenceDB([_ScalarResult(_tenant(tenant_id)), _ScalarResult(target_user)])

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        yield session

    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass, raising=False)

    result = tenants_api.assign_user_to_tenant(
        tenant_id=tenant_id,
        user_id=target_user.id,
        role="org_admin",
        current_user=current_user,
        db=fake_db,
    )

    with __import__("pytest").raises(Exception) as exc_info:
        __import__("asyncio").run(result)
    assert getattr(exc_info.value, "status_code", None) == 409
    assert target_user.tenant_id == other_tenant_id


def test_platform_admin_assign_user_rejects_ambiguous_case_insensitive_email(monkeypatch):
    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4())
    matching_users = [
        SimpleNamespace(id=uuid4(), email="Owner@example.com"),
        SimpleNamespace(id=uuid4(), email="owner@example.com"),
    ]
    fake_db = _SequenceDB([_ScalarResult(_tenant(tenant_id)), _ScalarResult(matching_users)])

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        yield session

    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass, raising=False)

    result = tenants_api.assign_user_to_tenant_by_email(
        tenant_id=tenant_id,
        data=tenants_api.TenantUserAssignment(email="owner@example.com", role="org_admin"),
        current_user=current_user,
        db=fake_db,
    )

    with __import__("pytest").raises(Exception) as exc_info:
        __import__("asyncio").run(result)
    assert getattr(exc_info.value, "status_code", None) == 409
    assert fake_db.added == []
