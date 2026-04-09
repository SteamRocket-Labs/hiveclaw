from __future__ import annotations

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


class _FakeDB:
    def __init__(self, tenant):
        self.tenant = tenant
        self.flushed = False

    async def execute(self, _stmt):
        return _ScalarResult(self.tenant)

    async def flush(self):
        self.flushed = True


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
