from __future__ import annotations

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


class _PermissionsDB:
    def __init__(self, *, agent, permissions=None):
        self.agent = agent
        self.permissions = permissions or []

    async def execute(self, stmt):
        sql = str(stmt)
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
