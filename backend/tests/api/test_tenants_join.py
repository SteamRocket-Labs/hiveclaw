from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


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
async def test_platform_admin_cannot_consume_invitation_before_database_access():
    import app.api.tenants as tenants_api

    current_user = SimpleNamespace(id=uuid4(), tenant_id=None, role="platform_admin")
    db = _FakeDB([])

    with pytest.raises(HTTPException) as exc_info:
        await tenants_api.join_company(
            data=tenants_api.JoinRequest(invitation_code="ADMINCODE1"),
            current_user=current_user,
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert db.statements == []
    assert db.added == []
    assert db.flushed is False
    assert db.committed is False


@pytest.mark.asyncio
async def test_platform_admin_role_observed_after_lock_cannot_consume_invitation(monkeypatch):
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    code = SimpleNamespace(
        code="ADMINCODE2",
        tenant_id=tenant_id,
        is_active=True,
        max_uses=1,
        used_count=0,
        granted_role="org_admin",
    )
    tenant = _tenant(tenant_id)
    locked_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        role="platform_admin",
        is_active=True,
    )
    request_user = SimpleNamespace(
        id=locked_user.id,
        tenant_id=None,
        role="member",
        is_active=True,
    )
    db = _FakeDB([_ScalarResult(code), _ScalarResult(tenant), _ScalarResult(locked_user)])

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        del reason, actor_id
        yield session

    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass, raising=False)

    with pytest.raises(HTTPException) as exc_info:
        await tenants_api.join_company(
            data=tenants_api.JoinRequest(invitation_code=code.code),
            current_user=request_user,
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert code.used_count == 0
    assert locked_user.tenant_id is None
    assert db.flushed is False
    assert db.committed is False
    assert not any(stmt.lstrip().upper().startswith("SET LOCAL") for stmt in db.statements)


@pytest.mark.asyncio
async def test_join_company_exact_replay_returns_current_membership_without_reconsuming(monkeypatch):
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    current_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        role="org_admin",
    )
    db = _FakeDB([_ScalarResult(uuid4()), _ScalarResult(_tenant(tenant_id))])
    token_calls: list[dict] = []

    def fake_create_access_token(user_id: str, role: str, tenant_id: str | None = None):
        token_calls.append({"user_id": user_id, "role": role, "tenant_id": tenant_id})
        return "recovered-membership-token"

    monkeypatch.setattr(tenants_api, "create_access_token", fake_create_access_token)

    result = await tenants_api.join_company(
        data=tenants_api.JoinRequest(invitation_code="  replaycode  "),
        current_user=current_user,
        db=db,
    )

    assert result.tenant.id == tenant_id
    assert result.role == "org_admin"
    assert result.access_token == "recovered-membership-token"
    assert token_calls == [
        {
            "user_id": str(current_user.id),
            "role": "org_admin",
            "tenant_id": str(tenant_id),
        }
    ]
    assert db.flushed is False
    assert db.committed is False
    lookup_params = [value for params in db.params for value in params.values()]
    assert "REPLAYCODE" in lookup_params
    assert tenant_id in lookup_params


@pytest.mark.asyncio
async def test_join_company_cross_tenant_replay_is_rejected_without_consumption():
    import app.api.tenants as tenants_api

    current_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        role="member",
    )
    db = _FakeDB([_ScalarResult(None)])

    with pytest.raises(HTTPException) as exc_info:
        await tenants_api.join_company(
            data=tenants_api.JoinRequest(invitation_code="OTHERCOMPANY"),
            current_user=current_user,
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "User already belongs to another company"
    assert db.flushed is False
    assert db.committed is False
    assert len(db._results) == 0


@pytest.mark.parametrize(
    ("granted_role", "existing_admin_count"),
    [
        pytest.param("member", 0, id="member-invite-without-existing-admin"),
        pytest.param("org_admin", 1, id="admin-invite-with-existing-admin"),
    ],
)
@pytest.mark.asyncio
async def test_join_company_uses_the_role_bound_to_the_invitation(
    monkeypatch,
    granted_role,
    existing_admin_count,
):
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    code = SimpleNamespace(
        code="ROLEBOUND1",
        tenant_id=tenant_id,
        is_active=True,
        max_uses=1,
        used_count=0,
        granted_role=granted_role,
    )
    tenant = _tenant(tenant_id)
    current_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        role="member",
        is_active=True,
        department_id=None,
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
        tokens_used_today=0,
        tokens_used_month=0,
        tokens_used_total=0,
        tokens_reset_at=None,
    )
    db = _FakeDB(
        [
            _ScalarResult(code),
            _ScalarResult(tenant),
            _ScalarResult(current_user),
            _ScalarResult(existing_admin_count),
        ]
    )

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        del reason, actor_id
        yield session

    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass, raising=False)
    monkeypatch.setattr(tenants_api, "create_access_token", lambda *_args, **_kwargs: "role-bound-token")

    result = await tenants_api.join_company(
        data=tenants_api.JoinRequest(invitation_code=code.code),
        current_user=current_user,
        db=db,
    )

    assert result.role == granted_role
    assert current_user.role == granted_role
    assert len(db._results) == 1
    assert db._results[0].scalar() == existing_admin_count


@pytest.mark.asyncio
async def test_self_create_company_pins_new_tenant_before_assigning_creator(monkeypatch):
    import app.api.tenants as tenants_api

    current_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        role="member",
        is_active=True,
        department_id=uuid4(),
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
        tokens_used_today=4,
        tokens_used_month=40,
        tokens_used_total=400,
        tokens_reset_at=datetime.now(timezone.utc),
    )
    db = _FakeDB([_ScalarResult(None), _ScalarResult(current_user)])
    bypass_calls: list[dict] = []
    bypass_exit_flushed: list[bool] = []
    bypass_exit_user_tenant: list[object] = []

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        bypass_calls.append({"session": session, "reason": reason, "actor_id": actor_id})
        try:
            yield session
        finally:
            bypass_exit_flushed.append(session.flushed)
            bypass_exit_user_tenant.append(current_user.tenant_id)

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
    assert current_user.department_id is None
    assert current_user.tokens_used_today == 0
    assert current_user.tokens_used_month == 0
    assert current_user.tokens_used_total == 0
    assert current_user.tokens_reset_at is None
    assert "FOR UPDATE" in db.statements[1]
    assert any(f"SET LOCAL app.current_tenant_id = '{current_user.tenant_id}'" in stmt for stmt in db.statements)
    assert db.flushed is True
    assert bypass_exit_flushed == [True]
    assert bypass_exit_user_tenant == [current_user.tenant_id]
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
    code = SimpleNamespace(
        code="Z53GS9R3",
        tenant_id=tenant_id,
        is_active=True,
        max_uses=5,
        used_count=0,
        granted_role="member",
    )
    tenant = _tenant(tenant_id)
    current_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        role="member",
        is_active=True,
        department_id=uuid4(),
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
        tokens_used_today=4,
        tokens_used_month=40,
        tokens_used_total=400,
        tokens_reset_at=datetime.now(timezone.utc),
    )
    db = _FakeDB([_ScalarResult(code), _ScalarResult(tenant), _ScalarResult(current_user)])
    bypass_calls: list[dict] = []
    bypass_exit_flushed: list[bool] = []
    token_calls: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        bypass_calls.append({"session": session, "reason": reason, "actor_id": actor_id})
        try:
            yield session
        finally:
            bypass_exit_flushed.append(session.flushed)

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
    assert current_user.department_id is None
    assert current_user.tokens_used_today == 0
    assert current_user.tokens_used_month == 0
    assert current_user.tokens_used_total == 0
    assert current_user.tokens_reset_at is None
    assert "FOR UPDATE" in db.statements[0]
    assert "FOR UPDATE" in db.statements[1]
    assert "FOR UPDATE" in db.statements[2]
    assert code.used_count == 1
    assert bypass_exit_flushed == [True]
    assert result.role == "member"
    assert result.access_token == "new-tenant-token"
    assert token_calls == [{"user_id": str(current_user.id), "role": "member", "tenant_id": str(tenant_id)}]
    assert db.flushed is True
    assert db.committed is True


@pytest.mark.asyncio
async def test_join_company_normalizes_invitation_code_before_lookup(monkeypatch):
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    code = SimpleNamespace(
        code="ABC12345",
        tenant_id=tenant_id,
        is_active=True,
        max_uses=5,
        used_count=0,
        granted_role="member",
    )
    tenant = _tenant(tenant_id)
    current_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        role="member",
        is_active=True,
        department_id=None,
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
        tokens_used_today=0,
        tokens_used_month=0,
        tokens_used_total=0,
        tokens_reset_at=None,
    )
    db = _FakeDB([_ScalarResult(code), _ScalarResult(tenant), _ScalarResult(current_user)])

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
