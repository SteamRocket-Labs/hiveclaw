from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


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


class _PermissionsDB:
    def __init__(self, *, agent, permissions=None):
        self.agent = agent
        self.permissions = permissions or []
        self.statements = []

    async def execute(self, stmt):
        sql = str(stmt)
        self.statements.append(sql)
        if "SET LOCAL app.current_tenant_id" in sql:
            return _ScalarResult(None)
        if "FROM agents" in sql:
            return _ScalarResult(self.agent)
        if "FROM agent_permissions" in sql:
            return _ListResult(self.permissions)
        raise AssertionError(f"Unhandled SQL in fake DB: {sql}")


@pytest.mark.asyncio
async def test_check_agent_access_falls_back_to_resource_permission_manage(monkeypatch):
    import app.core.permissions as permissions_module

    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id, role="member", tenant_id=tenant_id, department_id=None)
    db = _PermissionsDB(agent=agent)
    calls = []

    async def fake_check_permission(db_arg, **kwargs):
        calls.append((db_arg, kwargs))
        return kwargs["action"] == "manage"

    monkeypatch.setattr(permissions_module, "check_permission", fake_check_permission, raising=False)

    resolved_agent, access_level = await permissions_module.check_agent_access(db, user, agent_id)

    assert resolved_agent is agent
    assert access_level == "manage"
    assert calls[0][1]["principal_type"] == "user"
    assert calls[0][1]["resource_type"] == "agent"
    assert calls[0][1]["resource_id"] == agent_id


@pytest.mark.asyncio
async def test_check_agent_access_falls_back_to_resource_permission_execute(monkeypatch):
    import app.core.permissions as permissions_module

    tenant_id = uuid4()
    user_id = uuid4()
    department_id = uuid4()
    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id, role="member", tenant_id=tenant_id, department_id=department_id)
    db = _PermissionsDB(agent=agent)
    calls = []

    async def fake_check_permission(_db_arg, **kwargs):
        calls.append(kwargs)
        return kwargs["principal_type"] == "department" and kwargs["action"] == "execute"

    monkeypatch.setattr(permissions_module, "check_permission", fake_check_permission, raising=False)

    resolved_agent, access_level = await permissions_module.check_agent_access(db, user, agent_id)

    assert resolved_agent is agent
    assert access_level == "use"
    assert any(call["principal_type"] == "department" for call in calls)


@pytest.mark.asyncio
async def test_check_agent_access_prefers_current_owner_over_immutable_creator():
    import app.core.permissions as permissions_module

    tenant_id = uuid4()
    owner_id = uuid4()
    agent_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        creator_id=uuid4(),
        owner_user_id=owner_id,
        tenant_id=tenant_id,
    )
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=tenant_id, department_id=None)
    db = _PermissionsDB(agent=agent)

    resolved_agent, access_level = await permissions_module.check_agent_access(db, user, agent_id)

    assert resolved_agent is agent
    assert access_level == "manage"


@pytest.mark.asyncio
async def test_require_agent_manage_access_accepts_explicit_manage_grant(monkeypatch):
    import app.core.permissions as permissions_module

    agent = SimpleNamespace(id=uuid4())

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(permissions_module, "check_agent_access", fake_check_agent_access)

    assert await permissions_module.require_agent_manage_access(object(), object(), agent.id) is agent


@pytest.mark.asyncio
async def test_require_agent_manage_access_rejects_use_only_grant(monkeypatch):
    import app.core.permissions as permissions_module

    agent = SimpleNamespace(id=uuid4())

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(permissions_module, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc:
        await permissions_module.require_agent_manage_access(object(), object(), agent.id)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_check_agent_access_allows_org_admin_to_audit_same_tenant_private_agent():
    import app.core.permissions as permissions_module

    tenant_id = uuid4()
    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=tenant_id)
    user = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id, department_id=None)
    private_user_permission = SimpleNamespace(scope_type="user", scope_id=uuid4(), access_level="manage")
    db = _PermissionsDB(agent=agent, permissions=[private_user_permission])

    resolved_agent, access_level = await permissions_module.check_agent_access(db, user, agent_id)

    assert resolved_agent is agent
    assert access_level == "manage"


@pytest.mark.asyncio
async def test_check_agent_access_blocks_org_admin_from_other_tenant_agent():
    import app.core.permissions as permissions_module

    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=uuid4(), department_id=None)
    db = _PermissionsDB(agent=agent)

    with pytest.raises(HTTPException) as exc:
        await permissions_module.check_agent_access(db, user, agent_id)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_check_agent_access_fail_closes_for_tenantless_non_platform_user():
    import app.core.permissions as permissions_module

    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=None, department_id=None)
    db = _PermissionsDB(agent=agent)

    with pytest.raises(HTTPException) as exc:
        await permissions_module.check_agent_access(db, user, agent_id)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_check_agent_access_fail_closes_for_tenantless_agent_even_for_platform_admin():
    import app.core.permissions as permissions_module

    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=None)
    user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=None, department_id=None)
    db = _PermissionsDB(agent=agent)

    with pytest.raises(HTTPException) as exc:
        await permissions_module.check_agent_access(db, user, agent_id)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_check_agent_access_hides_soft_deleted_agent_before_role_grants():
    import app.core.permissions as permissions_module

    tenant_id = uuid4()
    agent_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        creator_id=uuid4(),
        tenant_id=tenant_id,
        deleted_at=object(),
        deactivated_at=None,
        sponsor=SimpleNamespace(is_active=True),
    )
    user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=tenant_id, department_id=None)
    db = _PermissionsDB(agent=agent)

    with pytest.raises(HTTPException) as exc:
        await permissions_module.check_agent_access(db, user, agent_id)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_check_agent_access_rejects_inactive_sponsor_agent():
    import app.core.permissions as permissions_module

    tenant_id = uuid4()
    agent_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        creator_id=uuid4(),
        tenant_id=tenant_id,
        deleted_at=None,
        deactivated_at=None,
        sponsor=SimpleNamespace(is_active=False),
    )
    user = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id, department_id=None)
    db = _PermissionsDB(agent=agent)

    with pytest.raises(HTTPException) as exc:
        await permissions_module.check_agent_access(db, user, agent_id)

    assert exc.value.status_code == 410
