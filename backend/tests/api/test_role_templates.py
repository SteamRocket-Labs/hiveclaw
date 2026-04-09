"""Tests for Role Template CRUD API (ARCHITECTURE.md Phase 5)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

import app.api.role_templates as rt_mod
from app.api.role_templates import router
from app.core.security import get_current_admin, get_current_user
from app.database import get_db


# ─── Fixtures ───────────────────────────────────────────

_TENANT_ID = uuid4()
_TEMPLATE_ID = uuid4()

_ADMIN_USER = SimpleNamespace(
    id=uuid4(), username="admin", role="org_admin", tenant_id=_TENANT_ID, is_active=True,
)

_EXISTING_TEMPLATE = SimpleNamespace(
    id=_TEMPLATE_ID, name="销售助理", description="销售部门模板",
    icon="💼", category="sales", soul_template="你是销售助理",
    default_skills=["crm"], department_id=None, model_id=None,
    tenant_id=_TENANT_ID, config_version=1, is_builtin=False, created_by=_ADMIN_USER.id,
)


class _ScalarsResult:
    def __init__(self, values):
        self._v = values

    def scalars(self):
        return self

    def all(self):
        return self._v


class _FakeDB:
    def __init__(self, *, templates=None, by_id=None):
        self._templates = templates or []
        self._by_id = by_id or {}
        self.added = []
        self.deleted = []

    async def execute(self, stmt):
        return _ScalarsResult(self._templates)

    async def get(self, model, pk):
        return self._by_id.get(pk)

    def add(self, obj):
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid4()
        self.added.append(obj)

    async def flush(self):
        pass

    async def delete(self, obj):
        self.deleted.append(obj)


def _build_client(*, templates=None, by_id=None):
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB(templates=templates, by_id=by_id)

    async def override_admin():
        return _ADMIN_USER

    async def override_user():
        return _ADMIN_USER

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_admin] = override_admin
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db


# ─── GET /role-templates ────────────────────────────────


def test_list_role_templates():
    client, _ = _build_client(templates=[_EXISTING_TEMPLATE])
    resp = client.get("/role-templates")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "销售助理"


def test_list_empty():
    client, _ = _build_client(templates=[])
    resp = client.get("/role-templates")
    assert resp.status_code == 200
    assert resp.json() == []


# ─── POST /role-templates ───────────────────────────────


def test_create_role_template():
    client, fake_db = _build_client()
    with patch.object(rt_mod, "bump_sync_version", new_callable=AsyncMock, return_value=2):
        resp = client.post("/role-templates", json={
            "name": "研发助理",
            "description": "研发部门默认模板",
            "category": "default",
            "soul_template": "你是研发助理",
        })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "研发助理"
    assert data["category"] == "default"
    assert len(fake_db.added) == 1


# ─── PATCH /role-templates/{id} ─────────────────────────


def test_update_role_template():
    client, _ = _build_client(by_id={_TEMPLATE_ID: _EXISTING_TEMPLATE})
    with patch.object(rt_mod, "bump_sync_version", new_callable=AsyncMock, return_value=3):
        resp = client.patch(f"/role-templates/{_TEMPLATE_ID}", json={"name": "新名字"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "新名字"
    assert _EXISTING_TEMPLATE.config_version == 2


def test_update_nonexistent_returns_404():
    client, _ = _build_client()
    resp = client.patch(f"/role-templates/{uuid4()}", json={"name": "x"})
    assert resp.status_code == 404


def test_update_other_tenant_returns_403():
    other_template = SimpleNamespace(
        id=uuid4(), tenant_id=uuid4(), is_builtin=False, config_version=1,
    )
    client, _ = _build_client(by_id={other_template.id: other_template})
    resp = client.patch(f"/role-templates/{other_template.id}", json={"name": "hijack"})
    assert resp.status_code == 403


def test_update_builtin_returns_403():
    builtin = SimpleNamespace(
        id=uuid4(), tenant_id=_TENANT_ID, is_builtin=True, config_version=1,
    )
    client, _ = _build_client(by_id={builtin.id: builtin})
    resp = client.patch(f"/role-templates/{builtin.id}", json={"name": "nope"})
    assert resp.status_code == 403


# ─── DELETE /role-templates/{id} ────────────────────────


def test_delete_role_template():
    client, fake_db = _build_client(by_id={_TEMPLATE_ID: _EXISTING_TEMPLATE})
    with patch.object(rt_mod, "bump_sync_version", new_callable=AsyncMock, return_value=4):
        resp = client.delete(f"/role-templates/{_TEMPLATE_ID}")
    assert resp.status_code == 204
    assert len(fake_db.deleted) == 1
