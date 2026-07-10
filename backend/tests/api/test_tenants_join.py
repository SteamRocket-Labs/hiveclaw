from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.statements: list[str] = []
        self.params: list[dict] = []
        self.added = []
        self.flushed = False
        self.committed = False

    async def execute(self, stmt):
        sql = getattr(stmt, "text", None) or str(stmt)
        self.statements.append(sql)
        if hasattr(stmt, "compile"):
            try:
                self.params.append(stmt.compile().params)
            except Exception:
                pass
        if sql.lstrip().upper().startswith("SET LOCAL"):
            return _ScalarResult(None)
        if not self._results:
            raise AssertionError(f"Unexpected DB execute: {sql}")
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid4()
            if getattr(value, "timezone", None) is None:
                value.timezone = "UTC"
            if getattr(value, "is_active", None) is None:
                value.is_active = True

    async def commit(self):
        self.committed = True


def _tenant(tenant_id):
    return SimpleNamespace(
        id=tenant_id,
        name="Acme",
        slug="acme",
        im_provider="web_only",
        timezone="UTC",
        is_active=True,
        created_at=datetime.now(timezone.utc),
        default_tokens_per_day=100,
        default_tokens_per_month=1000,
    )


@pytest.mark.asyncio
async def test_self_create_company_pins_new_tenant_before_assigning_creator(monkeypatch):
    import app.api.tenants as tenants_api

    current_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        role="member",
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
    )
    db = _FakeDB([_ScalarResult(None)])
    bypass_calls: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        bypass_calls.append({"session": session, "reason": reason, "actor_id": actor_id})
        yield session

    monkeypatch.setattr(tenants_api, "_slugify", lambda _name: "acme")
    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass, raising=False)

    result = await tenants_api.self_create_company(
        data=tenants_api.TenantCreate(name="Acme"),
        current_user=current_user,
        db=db,
    )

    assert result.name == "Acme"
    assert current_user.tenant_id is not None
    assert current_user.role == "org_admin"
    assert any(f"SET LOCAL app.current_tenant_id = '{current_user.tenant_id}'" in stmt for stmt in db.statements)
    assert db.flushed is True
    assert bypass_calls == [
        {
            "session": db,
            "reason": "self-service company creation",
            "actor_id": str(current_user.id),
        }
    ]


@pytest.mark.asyncio
async def test_join_company_uses_audited_bypass_for_invite_lookup_then_scopes_target_tenant(monkeypatch):
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    code = SimpleNamespace(code="Z53GS9R3", tenant_id=tenant_id, is_active=True, max_uses=5, used_count=0)
    tenant = _tenant(tenant_id)
    current_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        role="member",
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
    )
    db = _FakeDB([_ScalarResult(code), _ScalarResult(tenant), _ScalarResult(1)])
    bypass_calls: list[dict] = []
    token_calls: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        bypass_calls.append({"session": session, "reason": reason, "actor_id": actor_id})
        yield session

    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass, raising=False)

    def fake_create_access_token(user_id: str, role: str, tenant_id: str | None = None):
        token_calls.append({"user_id": user_id, "role": role, "tenant_id": tenant_id})
        return "new-tenant-token"

    monkeypatch.setattr(tenants_api, "create_access_token", fake_create_access_token, raising=False)

    result = await tenants_api.join_company(
        data=tenants_api.JoinRequest(invitation_code="Z53GS9R3"),
        current_user=current_user,
        db=db,
    )

    assert bypass_calls == [
        {
            "session": db,
            "reason": "tenant join invitation lookup",
            "actor_id": str(current_user.id),
        }
    ]
    assert any(f"SET LOCAL app.current_tenant_id = '{tenant_id}'" in stmt for stmt in db.statements)
    assert current_user.tenant_id == tenant_id
    assert current_user.quota_tokens_per_day == 100
    assert current_user.quota_tokens_per_month == 1000
    assert code.used_count == 1
    assert result.role == "member"
    assert result.access_token == "new-tenant-token"
    assert token_calls == [{"user_id": str(current_user.id), "role": "member", "tenant_id": str(tenant_id)}]
    assert db.flushed is True
    assert db.committed is True


@pytest.mark.asyncio
async def test_join_company_normalizes_invitation_code_before_lookup(monkeypatch):
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    code = SimpleNamespace(code="ABC12345", tenant_id=tenant_id, is_active=True, max_uses=5, used_count=0)
    tenant = _tenant(tenant_id)
    current_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        role="member",
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
    )
    db = _FakeDB([_ScalarResult(code), _ScalarResult(tenant), _ScalarResult(1)])

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        del reason, actor_id
        yield session

    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass, raising=False)

    await tenants_api.join_company(
        data=tenants_api.JoinRequest(invitation_code="  abc12345  "),
        current_user=current_user,
        db=db,
    )

    lookup_params = [value for params in db.params for value in params.values()]
    assert "ABC12345" in lookup_params
    assert "  abc12345  " not in lookup_params
