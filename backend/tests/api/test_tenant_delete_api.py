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


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.flushed = False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    async def flush(self):
        self.flushed = True


def _tenant(tenant_id):
    return SimpleNamespace(
        id=tenant_id,
        name="Acme",
        slug="acme",
        im_provider="web_only",
        timezone="UTC",
        is_active=True,
        created_at=None,
    )


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
    db = _FakeDB([
        _ScalarResult(target_tenant),
        _ListResult([running_agent]),
        _ListResult([current_user, member]),
    ])

    result = await tenants_api.delete_tenant(
        tenant_id=tenant_id,
        current_user=current_user,
        db=db,
    )

    assert result.fallback_tenant_id is None
    assert result.needs_company_setup is True
    assert target_tenant.is_active is False
    assert running_agent.status == "paused"
    assert current_user.tenant_id is None
    assert current_user.department_id is None
    assert current_user.role == "member"
    assert member.tenant_id is None
    assert member.department_id is None
    assert member.role == "member"
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
    )
    another_platform_admin = SimpleNamespace(
        id=uuid4(),
        role="platform_admin",
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
    fallback_tenant = _tenant(fallback_tenant_id)
    db = _FakeDB([
        _ScalarResult(target_tenant),
        _ScalarResult(fallback_tenant),
        _ListResult([running_agent]),
        _ListResult([current_user, another_platform_admin, member]),
    ])

    result = await tenants_api.delete_tenant(
        tenant_id=tenant_id,
        current_user=current_user,
        db=db,
    )

    assert result.fallback_tenant_id == fallback_tenant_id
    assert result.needs_company_setup is False
    assert target_tenant.is_active is False
    assert running_agent.status == "paused"
    assert current_user.tenant_id == fallback_tenant_id
    assert current_user.department_id is None
    assert current_user.role == "platform_admin"
    assert another_platform_admin.tenant_id == fallback_tenant_id
    assert another_platform_admin.department_id is None
    assert another_platform_admin.role == "platform_admin"
    assert member.tenant_id is None
    assert member.department_id is None
    assert member.role == "member"
    assert db.flushed is True
