from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)

    def all(self):
        return self._values


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.flushed = False
        self.statements = []
        self.added = []
        self.commits = 0

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    async def flush(self):
        self.flushed = True

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1


def _tenant(tenant_id):
    return SimpleNamespace(
        id=tenant_id,
        name="Acme",
        slug="acme",
        im_provider="web_only",
        timezone="UTC",
        is_active=True,
        default_tokens_per_day=100,
        default_tokens_per_month=1000,
        created_at=None,
    )


def _patch_platform_bypass(monkeypatch, tenants_api, db):
    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        assert session is db
        assert reason == "platform-admin delete tenant"
        assert actor_id is not None
        yield session

    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass)


def test_tenant_retirement_is_wired_as_an_optional_delete_body() -> None:
    from app.main import app

    operation = app.openapi()["paths"]["/api/tenants/{tenant_id}"]["delete"]
    request_body = operation["requestBody"]

    assert "required" not in request_body
    assert request_body["content"]["application/json"]["schema"]["anyOf"] == [
        {"$ref": "#/components/schemas/TenantRetirementRequest"},
        {"type": "null"},
    ]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/TenantDeleteOut"
    }


@pytest.mark.asyncio
async def test_org_admin_delete_own_tenant_detaches_users_and_requires_setup():
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    current_user = SimpleNamespace(
        id=uuid4(),
        role="org_admin",
        tenant_id=tenant_id,
        department_id=uuid4(),
    )
    member = SimpleNamespace(
        id=uuid4(),
        role="member",
        tenant_id=tenant_id,
        department_id=uuid4(),
    )
    running_agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, status="running")
    target_tenant = _tenant(tenant_id)
    db = _FakeDB(
        [
            _ListResult([target_tenant]),
            _ListResult([current_user, member]),
            _ListResult([running_agent]),
            _ListResult([]),  # scrub_tenant_tool_secrets: no tool-config overrides
            _ListResult([]),  # scrub_tenant_channel_secrets: no agent channels
            _ListResult([]),  # scrub_tenant_channel_secrets: no tenant channels
            _ListResult([]),  # scrub_tenant_channel_secrets: no chat targets
            _ListResult([]),  # scrub_tenant_channel_secrets: no outbox targets
            _ListResult([]),  # scrub_tenant_channel_secrets: no budget targets
            _ListResult([]),  # scrub_tenant_channel_secrets: no schedule targets
            _ListResult([]),  # scrub_tenant_channel_secrets: no trigger targets
            _ListResult([]),  # scrub_tenant_channel_secrets: no runtime-task targets
            _ListResult([]),  # scrub_tenant_channel_secrets: no ingress payloads
        ]
    )

    result = await tenants_api.delete_tenant(
        tenant_id=tenant_id,
        current_user=current_user,
        db=db,
    )

    assert result.fallback_tenant_id is None
    assert result.needs_company_setup is True
    assert target_tenant.is_active is False
    assert running_agent.status == "stopped"
    assert current_user.tenant_id is None
    assert current_user.department_id is None
    assert current_user.role == "member"
    assert member.tenant_id is None
    assert member.department_id is None
    assert member.role == "member"
    assert "FOR UPDATE" in db.statements[0]
    assert "FOR UPDATE" in db.statements[1]
    assert db.flushed is True


@pytest.mark.asyncio
async def test_platform_admin_delete_tenant_returns_fallback_and_rehomes_platform_admins():
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    fallback_tenant_id = uuid4()
    current_user = SimpleNamespace(
        id=uuid4(),
        role="platform_admin",
        tenant_id=tenant_id,
        department_id=uuid4(),
        quota_tokens_per_day=999,
        quota_tokens_per_month=9999,
        tokens_used_today=3,
        tokens_used_month=30,
        tokens_used_total=300,
        tokens_reset_at=object(),
    )
    another_platform_admin = SimpleNamespace(
        id=uuid4(),
        role="platform_admin",
        tenant_id=tenant_id,
        department_id=uuid4(),
        quota_tokens_per_day=999,
        quota_tokens_per_month=9999,
        tokens_used_today=4,
        tokens_used_month=40,
        tokens_used_total=400,
        tokens_reset_at=object(),
    )
    member = SimpleNamespace(
        id=uuid4(),
        role="member",
        tenant_id=tenant_id,
        department_id=uuid4(),
    )
    running_agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, status="running")
    target_tenant = _tenant(tenant_id)
    fallback_tenant = _tenant(fallback_tenant_id)
    db = _FakeDB(
        [
            _ListResult([target_tenant, fallback_tenant]),
            _ListResult([current_user, another_platform_admin, member]),
            _ListResult([running_agent]),
            _ListResult([]),  # scrub_tenant_tool_secrets: no tool-config overrides
            _ListResult([]),  # scrub_tenant_channel_secrets: no agent channels
            _ListResult([]),  # scrub_tenant_channel_secrets: no tenant channels
            _ListResult([]),  # scrub_tenant_channel_secrets: no chat targets
            _ListResult([]),  # scrub_tenant_channel_secrets: no outbox targets
            _ListResult([]),  # scrub_tenant_channel_secrets: no budget targets
            _ListResult([]),  # scrub_tenant_channel_secrets: no schedule targets
            _ListResult([]),  # scrub_tenant_channel_secrets: no trigger targets
            _ListResult([]),  # scrub_tenant_channel_secrets: no runtime-task targets
            _ListResult([]),  # scrub_tenant_channel_secrets: no ingress payloads
        ]
    )
    bypass_calls: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        bypass_calls.append({"session": session, "reason": reason, "actor_id": actor_id})
        yield session

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tenants_api, "enter_rls_bypass", fake_enter_rls_bypass, raising=False)

    try:
        result = await tenants_api.delete_tenant(
            tenant_id=tenant_id,
            current_user=current_user,
            db=db,
        )
    finally:
        monkeypatch.undo()

    assert result.fallback_tenant_id == fallback_tenant_id
    assert result.needs_company_setup is False
    assert target_tenant.is_active is False
    assert running_agent.status == "stopped"
    assert current_user.tenant_id == fallback_tenant_id
    assert current_user.department_id is None
    assert current_user.role == "platform_admin"
    assert current_user.quota_tokens_per_day == 100
    assert current_user.quota_tokens_per_month == 1000
    assert current_user.tokens_used_today == 0
    assert current_user.tokens_used_month == 0
    assert current_user.tokens_used_total == 0
    assert current_user.tokens_reset_at is None
    assert another_platform_admin.tenant_id == fallback_tenant_id
    assert another_platform_admin.department_id is None
    assert another_platform_admin.role == "platform_admin"
    assert another_platform_admin.quota_tokens_per_day == 100
    assert another_platform_admin.quota_tokens_per_month == 1000
    assert another_platform_admin.tokens_used_today == 0
    assert another_platform_admin.tokens_used_month == 0
    assert another_platform_admin.tokens_used_total == 0
    assert another_platform_admin.tokens_reset_at is None
    assert member.tenant_id is None
    assert member.department_id is None
    assert member.role == "member"
    assert "FOR UPDATE" in db.statements[0]
    assert "FOR UPDATE" in db.statements[1]
    assert db.flushed is True
    assert bypass_calls == [
        {
            "session": db,
            "reason": "platform-admin delete tenant",
            "actor_id": str(current_user.id),
        }
    ]


@pytest.mark.asyncio
async def test_platform_admin_exact_retirement_deactivates_last_admin_after_owned_agent_soft_delete(monkeypatch):
    import app.api.tenants as tenants_api
    from app.services import channel_secret_storage, tool_config_service
    from app.services.user_offboarding_service import AuthorityRevocationReceipt

    tenant_id = uuid4()
    fallback_tenant_id = uuid4()
    owner = SimpleNamespace(
        id=uuid4(),
        role="platform_admin",
        tenant_id=tenant_id,
        department_id=uuid4(),
        is_active=True,
        quota_tokens_per_day=999,
        quota_tokens_per_month=9999,
        tokens_used_today=1,
        tokens_used_month=2,
        tokens_used_total=3,
        tokens_reset_at=object(),
    )
    last_admin = SimpleNamespace(
        id=uuid4(),
        role="org_admin",
        tenant_id=tenant_id,
        department_id=uuid4(),
        is_active=True,
    )
    retired_agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=last_admin.id,
        creator_id=last_admin.id,
        status="stopped",
        deleted_at=object(),
    )
    target_tenant = _tenant(tenant_id)
    fallback_tenant = _tenant(fallback_tenant_id)
    db = _FakeDB(
        [
            _ListResult([]),
            _ListResult([target_tenant, fallback_tenant]),
            _ScalarResult(None),
            _ListResult([owner, last_admin]),
            _ListResult([retired_agent]),
        ]
    )
    revoked = []
    published = []

    async def fake_revoke(_db, *, target_user, actor_user, now):
        assert _db is db
        assert actor_user is owner
        revoked.append((target_user.id, now))
        return AuthorityRevocationReceipt(
            refresh_tokens=2,
            local_bridge_connections=1,
            runtime_tasks=1,
        )

    async def fake_publish(*, user_id, revocations):
        assert db.commits == 1
        published.append((user_id, revocations))

    async def no_scrub(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(tenants_api, "revoke_user_authority", fake_revoke, raising=False)
    monkeypatch.setattr(
        tenants_api,
        "publish_user_authority_runtime_cancellations",
        fake_publish,
        raising=False,
    )
    monkeypatch.setattr(tool_config_service, "scrub_tenant_tool_secrets", no_scrub)
    monkeypatch.setattr(channel_secret_storage, "scrub_tenant_channel_secrets", no_scrub)
    _patch_platform_bypass(monkeypatch, tenants_api, db)

    result = await tenants_api.delete_tenant(
        tenant_id=tenant_id,
        retirement=tenants_api.TenantRetirementRequest(
            expected_user_ids=[last_admin.id],
            reason="Weekend RC fixture cleanup",
            request_id="retire-fixture-1",
        ),
        current_user=owner,
        db=db,
    )

    assert result.retirement_status == "retired"
    assert result.retirement_request_id == "retire-fixture-1"
    # Lock ordering proof: the claimable-pairing pre-lock is the first
    # retirement statement, before any identity (tenant/user/agent) lock,
    # so an in-flight exchange can never be waiting on identity locks
    # this transaction holds.
    assert "local_agent_bridge_pairing_sessions" in db.statements[0]
    assert "FOR UPDATE" in db.statements[0]
    assert "FROM tenants" in db.statements[1]
    assert [row.user_id for row in result.retired_users] == [last_admin.id]
    assert result.retired_users[0].is_active is False
    assert result.retired_users[0].revocations["refresh_tokens"] == 2
    assert result.retired_users[0].revocations["local_bridge_connections"] == 1
    assert last_admin.is_active is False
    assert last_admin.tenant_id == tenant_id
    assert last_admin.role == "org_admin"
    assert owner.is_active is True
    assert owner.role == "platform_admin"
    assert owner.tenant_id == fallback_tenant_id
    assert target_tenant.is_active is False
    assert revoked[0][0] == last_admin.id
    assert published[0][0] == last_admin.id
    assert db.commits == 1
    audit = next(row for row in db.added if row.action == "tenant:retired")
    assert audit.details["expected_user_ids"] == [str(last_admin.id)]
    assert audit.details["request_id"] == "retire-fixture-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["missing_user", "foreign_user", "platform_admin"])
async def test_tenant_retirement_rejects_stale_or_protected_user_set_before_any_effect(monkeypatch, mismatch):
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    owner = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=None)
    admin = SimpleNamespace(
        id=uuid4(),
        role="org_admin",
        tenant_id=tenant_id,
        department_id=None,
        is_active=True,
    )
    member = SimpleNamespace(
        id=uuid4(),
        role="member",
        tenant_id=tenant_id,
        department_id=None,
        is_active=True,
    )
    protected_admin = SimpleNamespace(
        id=uuid4(),
        role="platform_admin",
        tenant_id=tenant_id,
        department_id=None,
        is_active=True,
    )
    target_tenant = _tenant(tenant_id)
    db = _FakeDB(
        [
            _ListResult([]),
            _ListResult([target_tenant]),
            _ScalarResult(None),
            _ListResult([admin, member, protected_admin]),
        ]
    )

    async def unexpected_revoke(*_args, **_kwargs):
        raise AssertionError("stale retirement input must not revoke authority")

    monkeypatch.setattr(tenants_api, "revoke_user_authority", unexpected_revoke, raising=False)
    _patch_platform_bypass(monkeypatch, tenants_api, db)
    if mismatch == "missing_user":
        expected_user_ids = [admin.id]
    elif mismatch == "foreign_user":
        expected_user_ids = [admin.id, member.id, uuid4()]
    else:
        expected_user_ids = [admin.id, member.id, protected_admin.id]

    with pytest.raises(tenants_api.HTTPException) as exc_info:
        await tenants_api.delete_tenant(
            tenant_id=tenant_id,
            retirement=tenants_api.TenantRetirementRequest(
                expected_user_ids=expected_user_ids,
                reason="Weekend RC fixture cleanup",
                request_id="retire-fixture-stale",
            ),
            current_user=owner,
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "tenant_retirement_user_set_stale"
    assert target_tenant.is_active is True
    assert admin.is_active is True
    assert member.is_active is True
    assert db.added == []
    assert db.commits == 0
    assert exc_info.value.detail["protected_user_ids"] == (
        [str(protected_admin.id)] if mismatch == "platform_admin" else []
    )


@pytest.mark.asyncio
async def test_tenant_retirement_rejects_live_owned_agent_before_any_effect(monkeypatch):
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    owner = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=None)
    admin = SimpleNamespace(
        id=uuid4(),
        role="org_admin",
        tenant_id=tenant_id,
        department_id=None,
        is_active=True,
    )
    active_agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=admin.id,
        creator_id=admin.id,
        status="idle",
        deleted_at=None,
    )
    target_tenant = _tenant(tenant_id)
    db = _FakeDB(
        [
            _ListResult([]),
            _ListResult([target_tenant]),
            _ScalarResult(None),
            _ListResult([admin]),
            _ListResult([active_agent]),
        ]
    )

    async def unexpected_revoke(*_args, **_kwargs):
        raise AssertionError("owned Agent blocker must be checked before revocation")

    monkeypatch.setattr(tenants_api, "revoke_user_authority", unexpected_revoke, raising=False)
    _patch_platform_bypass(monkeypatch, tenants_api, db)

    with pytest.raises(tenants_api.HTTPException) as exc_info:
        await tenants_api.delete_tenant(
            tenant_id=tenant_id,
            retirement=tenants_api.TenantRetirementRequest(
                expected_user_ids=[admin.id],
                reason="Weekend RC fixture cleanup",
                request_id="retire-fixture-agent-blocked",
            ),
            current_user=owner,
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {
        "code": "tenant_retirement_owned_agents_active",
        "agent_ids": [str(active_agent.id)],
    }
    assert target_tenant.is_active is True
    assert admin.is_active is True
    assert active_agent.status == "idle"
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_tenant_retirement_replay_is_exact_and_has_no_repeated_effect(monkeypatch):
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    user_id = uuid4()
    owner = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=None)
    target_tenant = _tenant(tenant_id)
    target_tenant.is_active = False
    audit = SimpleNamespace(
        details={
            "schema": "hive.tenant_retirement.v1",
            "request_id": "retire-fixture-replay",
            "reason": "Weekend RC fixture cleanup",
            "expected_user_ids": [str(user_id)],
            "fallback_tenant_id": None,
            "retired_users": [
                {
                    "user_id": str(user_id),
                    "status": "deactivated",
                    "is_active": False,
                    "revocations": {"refresh_tokens": 2, "local_bridge_connections": 1},
                }
            ],
        }
    )
    db = _FakeDB([_ListResult([]), _ListResult([target_tenant]), _ScalarResult(audit)])

    async def unexpected_revoke(*_args, **_kwargs):
        raise AssertionError("replay must not repeat retirement effects")

    monkeypatch.setattr(tenants_api, "revoke_user_authority", unexpected_revoke, raising=False)
    _patch_platform_bypass(monkeypatch, tenants_api, db)

    result = await tenants_api.delete_tenant(
        tenant_id=tenant_id,
        retirement=tenants_api.TenantRetirementRequest(
            expected_user_ids=[user_id],
            reason=" Weekend RC fixture cleanup ",
            request_id=" retire-fixture-replay ",
        ),
        current_user=owner,
        db=db,
    )

    assert result.retirement_status == "already_retired"
    assert result.retired_users[0].user_id == user_id
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_tenant_retirement_rejects_request_id_reuse_with_changed_input(monkeypatch):
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    user_id = uuid4()
    owner = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=None)
    target_tenant = _tenant(tenant_id)
    audit = SimpleNamespace(
        details={
            "request_id": "retire-fixture-conflict",
            "reason": "Original reason",
            "expected_user_ids": [str(user_id)],
        }
    )
    db = _FakeDB([_ListResult([]), _ListResult([target_tenant]), _ScalarResult(audit)])
    _patch_platform_bypass(monkeypatch, tenants_api, db)

    with pytest.raises(tenants_api.HTTPException) as exc_info:
        await tenants_api.delete_tenant(
            tenant_id=tenant_id,
            retirement=tenants_api.TenantRetirementRequest(
                expected_user_ids=[user_id],
                reason="Changed reason",
                request_id="retire-fixture-conflict",
            ),
            current_user=owner,
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "tenant_retirement_idempotency_conflict"
    assert target_tenant.is_active is True
    assert db.added == []


@pytest.mark.asyncio
async def test_org_admin_cannot_request_platform_tenant_retirement():
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    actor = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    db = _FakeDB([])

    with pytest.raises(tenants_api.HTTPException) as exc_info:
        await tenants_api.delete_tenant(
            tenant_id=tenant_id,
            retirement=tenants_api.TenantRetirementRequest(
                expected_user_ids=[actor.id],
                reason="Attempted self retirement",
                request_id="retire-org-admin",
            ),
            current_user=actor,
            db=db,
        )

    assert exc_info.value.status_code == 403
    assert db.statements == []
