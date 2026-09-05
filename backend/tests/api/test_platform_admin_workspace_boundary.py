from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.security import get_current_admin, get_current_user
from app.database import get_db


class _BombDB:
    sync_session = SimpleNamespace(info={})

    async def execute(self, _statement):
        raise AssertionError("platform-admin company route must fail before DB access")


def _platform_admin_boundary_client() -> TestClient:
    import app.api.agents as agents_api
    import app.api.enterprise as enterprise_api
    import app.api.external_principals as external_principals_api
    import app.api.guard_policies as guard_policies_api
    import app.api.knowledge_company as knowledge_company_api
    import app.api.users as users_api

    app = FastAPI()
    for router in (
        enterprise_api.router,
        users_api.router,
        external_principals_api.router,
        guard_policies_api.router,
        knowledge_company_api.router,
        agents_api.router,
    ):
        app.include_router(router)

    user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4(), department_id=None)

    async def override_user():
        return user

    async def override_db():
        yield _BombDB()

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_current_admin] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False)


def _member_boundary_client() -> TestClient:
    import app.api.agents as agents_api
    import app.api.enterprise as enterprise_api
    import app.api.external_principals as external_principals_api
    import app.api.guard_policies as guard_policies_api
    import app.api.knowledge_company as knowledge_company_api
    import app.api.users as users_api

    app = FastAPI()
    for router in (
        enterprise_api.router,
        users_api.router,
        external_principals_api.router,
        guard_policies_api.router,
        knowledge_company_api.router,
        agents_api.router,
    ):
        app.include_router(router)

    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), department_id=None)

    async def override_user():
        return user

    async def override_db():
        yield _BombDB()

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_current_admin] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False)


# PDEC-013: platform administrators exercise company business inside a valid
# company context instead of being denied before data access. The bomb DB
# proves the request passes the role gate and reaches the actual company
# scope (any non-403 outcome here means the gate opened; the AssertionError
# surfaces as a 500 because raise_server_exceptions=False).
_BUSINESS_BODY_PATHS = (
    "/enterprise/approvals",
    "/enterprise/stats",
    "/enterprise/org/departments",
    "/enterprise/org/members",
    "/enterprise/legacy-company-files/status",
    "/enterprise/invitation-codes",
    "/users/",
    "/enterprise/external-principals",
    "/guard-policies",
    "/knowledge/company/source-contracts",
    "/agents/system/hr",
)

# The platform-key allowlist for system settings is an intentional role/key
# boundary, not an obsolete business denial.
_SETTING_ALLOWLIST_PATHS = ("/enterprise/system-settings/feishu_org_sync",)


# Routes that genuinely require an administrator role. The remaining business
# body routes are tenant-scoped surfaces a member may legitimately query with
# their own-asset filters, so they are not part of the member negative here.
_MEMBER_DENIED_PATHS = (
    "/users/",
    "/enterprise/external-principals",
    "/enterprise/invitation-codes",
    "/guard-policies",
    "/knowledge/company/source-contracts",
    "/enterprise/system-settings/feishu_org_sync",
)


@pytest.mark.parametrize("path", _MEMBER_DENIED_PATHS)
def test_member_company_workspace_reads_fail_before_db(path: str) -> None:
    with _member_boundary_client() as client:
        response = client.get(path)

    assert response.status_code == 403


@pytest.mark.parametrize("path", _BUSINESS_BODY_PATHS)
def test_platform_admin_company_business_passes_role_gate(path: str) -> None:
    with _platform_admin_boundary_client() as client:
        response = client.get(path)

    assert response.status_code != 403


@pytest.mark.parametrize("path", _SETTING_ALLOWLIST_PATHS)
def test_platform_admin_platform_setting_allowlist_still_bounded(path: str) -> None:
    with _platform_admin_boundary_client() as client:
        response = client.get(path)

    assert response.status_code == 403


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _SettingDB:
    def __init__(self, results=()):
        self.results = list(results)
        self.statements = []
        self.committed = False
        self.sync_session = SimpleNamespace(info={})

    async def execute(self, statement):
        self.statements.append(statement)
        if str(statement).lstrip().upper().startswith("SET LOCAL"):
            return _ScalarResult(None)
        return self.results.pop(0)

    def add(self, _value):
        raise AssertionError("test expects an existing setting")

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_feishu_org_setting_get_projects_secret_presence_without_secret() -> None:
    import app.api.enterprise as enterprise_api

    tenant_id = uuid4()
    setting = SimpleNamespace(
        key="feishu_org_sync",
        value={"app_id": "synthetic-app", "app_secret": "synthetic-secret"},
        updated_at=datetime.now(UTC),
    )
    db = _SettingDB([_ScalarResult(setting)])

    result = await enterprise_api.get_system_setting(
        key="feishu_org_sync",
        tenant_id=str(tenant_id),
        current_user=SimpleNamespace(role="org_admin", tenant_id=tenant_id),
        db=db,
    )

    assert result["value"] == {"app_id": "synthetic-app", "app_secret_configured": True}
    assert "app_secret" not in result["value"]


@pytest.mark.asyncio
async def test_missing_feishu_org_setting_reports_secret_not_configured() -> None:
    import app.api.enterprise as enterprise_api

    tenant_id = uuid4()
    db = _SettingDB([_ScalarResult(None)])

    result = await enterprise_api.get_system_setting(
        key="feishu_org_sync",
        tenant_id=str(tenant_id),
        current_user=SimpleNamespace(role="org_admin", tenant_id=tenant_id),
        db=db,
    )

    assert result["value"] == {"app_secret_configured": False}


@pytest.mark.asyncio
async def test_feishu_org_setting_put_never_echoes_secret_but_preserves_stored_value() -> None:
    import app.api.enterprise as enterprise_api

    tenant_id = uuid4()
    setting = SimpleNamespace(key="feishu_org_sync", value={}, updated_at=None)
    db = _SettingDB([_ScalarResult(setting)])

    result = await enterprise_api.update_system_setting(
        key="feishu_org_sync",
        data=enterprise_api.SettingUpdate(value={"app_id": "synthetic-app", "app_secret": "synthetic-secret"}),
        tenant_id=str(tenant_id),
        current_user=SimpleNamespace(role="org_admin", tenant_id=tenant_id),
        db=db,
    )

    assert setting.value["app_secret"] == "synthetic-secret"
    assert result["value"] == {"app_id": "synthetic-app", "app_secret_configured": True}
    assert db.committed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "key"),
    (
        ("platform_admin", "feishu_org_sync"),
        ("platform_admin", "unknown_global_key"),
        ("org_admin", "platform"),
        ("org_admin", "notification_bar"),
    ),
)
async def test_system_setting_role_key_allowlist_fails_before_db(role: str, key: str) -> None:
    import app.api.enterprise as enterprise_api

    db = _SettingDB()
    with pytest.raises(HTTPException) as exc_info:
        await enterprise_api.get_system_setting(
            key=key,
            tenant_id=str(uuid4()),
            current_user=SimpleNamespace(role=role, tenant_id=uuid4()),
            db=db,
        )

    assert exc_info.value.status_code == 403
    assert db.statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ("notification_bar", "platform"))
async def test_platform_admin_keeps_exact_platform_setting_keys(key: str) -> None:
    import app.api.enterprise as enterprise_api

    setting = SimpleNamespace(key=key, value={"enabled": True}, updated_at=None)
    db = _SettingDB([_ScalarResult(setting)])

    result = await enterprise_api.get_system_setting(
        key=key,
        tenant_id=None,
        current_user=SimpleNamespace(role="platform_admin", tenant_id=uuid4()),
        db=db,
    )

    assert result["key"] == key
    assert result["value"] == {"enabled": True}


class _MappingsResult:
    def __init__(self, *, mappings=(), scalars=()):
        self._mappings = list(mappings)
        self._scalars = list(scalars)

    def mappings(self):
        return SimpleNamespace(all=lambda: self._mappings)

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars)


class _AgentListDB:
    def __init__(self, results=()):
        self.statements = []
        self.results = list(results)
        self.sync_session = SimpleNamespace(info={})

    async def execute(self, statement):
        self.statements.append(statement)
        if str(statement).lstrip().upper().startswith("SET LOCAL"):
            return _ScalarResult(None)
        if self.results:
            return self.results.pop(0)
        return _MappingsResult()


@pytest.mark.asyncio
async def test_platform_admin_agent_inventory_lists_managed_company_business() -> None:
    """PDEC-013: the platform administrator inventory is the selected
    company's full business inventory with manage projection — no permission
    rows are consulted and no operator shell is produced."""

    import app.api.agents as agents_api

    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    agent_row = {
        "id": agent_id,
        "name": "Employee agent",
        "avatar_url": None,
        "role_description": "employee helper",
        "status": "idle",
        "creator_id": uuid4(),
        "sponsor_user_id": uuid4(),
        "participant_id": uuid4(),
        "owner_user_id": uuid4(),
        "tenant_id": tenant_id,
        "primary_model_id": None,
        "fallback_model_id": None,
        "tokens_used_today": 0,
        "tokens_used_month": 0,
        "tokens_used_total": 0,
        "max_tool_rounds": 200,
        "max_triggers": 20,
        "min_poll_interval_min": 5,
        "webhook_rate_limit": 5,
        "last_heartbeat_at": None,
        "timezone": "UTC",
        "agent_type": "native",
        "agent_class": "internal_tenant",
        "security_zone": "standard",
        "execution_mode": "standard",
        "created_at": datetime.now(UTC),
        "last_active_at": None,
        "deleted_at": None,
        "deactivated_at": None,
        "deactivation_reason": None,
    }
    db = _AgentListDB([_MappingsResult(mappings=[agent_row]), _MappingsResult(scalars=[])])

    result = await agents_api.list_agents(
        tenant_id=tenant_id,
        current_user=SimpleNamespace(
            id=user_id,
            role="platform_admin",
            tenant_id=tenant_id,
            department_id=uuid4(),
        ),
        db=db,
    )

    assert len(result) == 1
    payload = result[0].model_dump()
    assert payload["access_level"] == "manage"
    assert payload.get("operator_shell") is not True
    assert not any("agent_permissions.scope_type" in str(stmt) for stmt in db.statements)


@pytest.mark.asyncio
async def test_member_agent_inventory_still_requires_explicit_scope() -> None:
    import app.api.agents as agents_api

    tenant_id = uuid4()
    user_id = uuid4()
    db = _AgentListDB()

    result = await agents_api.list_agents(
        tenant_id=None,
        current_user=SimpleNamespace(
            id=user_id,
            role="member",
            tenant_id=tenant_id,
            department_id=None,
        ),
        db=db,
    )

    assert result == []
    statement = next(stmt for stmt in db.statements if "agent_permissions.scope_type" in str(stmt))
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "agent_permissions.scope_type = 'user'" in sql
    assert user_id.hex in sql


@pytest.mark.asyncio
async def test_member_company_permission_does_not_widen_operator_list_shell() -> None:
    import app.api.agents as agents_api

    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    operator_grant = SimpleNamespace(
        resource_id=agent_id,
        actions=[agents_api.AGENT_OPERATOR_INSPECT_ACTION],
        conditions={
            "operator_inspection": {
                "schema": agents_api.AGENT_OPERATOR_INSPECTION_SCHEMA,
            }
        },
        effect="allow",
        revoked_at=None,
        expires_at=None,
    )
    agent_row = {
        "id": agent_id,
        "name": "Audited agent",
        "avatar_url": None,
        "status": "idle",
        "creator_id": uuid4(),
        "owner_user_id": uuid4(),
        "agent_type": "native",
        "agent_class": "internal_tenant",
    }
    db = _AgentListDB(
        [
            _MappingsResult(scalars=[operator_grant]),
            _MappingsResult(mappings=[agent_row]),
            _MappingsResult(scalars=[]),
        ]
    )

    result = await agents_api.list_agents(
        tenant_id=None,
        current_user=SimpleNamespace(
            id=user_id,
            role="member",
            tenant_id=tenant_id,
            department_id=None,
        ),
        db=db,
    )

    assert len(result) == 1
    payload = result[0].model_dump()
    assert payload["access_level"] == "operator"
    assert payload["operator_shell"] is True
    assert "tenant_id" not in payload
    assert "primary_model_id" not in payload
