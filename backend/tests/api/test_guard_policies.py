"""Tests for Guard Policy API + bootstrap policy integration (ARCHITECTURE.md §7.4)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

import app.api.guard_policies as gp_mod
from app.api.guard_policies import router as gp_router
from app.core.security import get_current_admin
from app.database import get_db


# ─── Fixtures ───────────────────────────────────────────

_TENANT_ID = uuid4()
_POLICY_ID = uuid4()

_ADMIN_USER = SimpleNamespace(
    id=uuid4(),
    username="admin",
    email="admin@test.com",
    display_name="管理员",
    role="org_admin",
    tenant_id=_TENANT_ID,
    is_active=True,
)

_MEMBER_USER = SimpleNamespace(
    id=uuid4(),
    username="member",
    email="member@test.com",
    display_name="普通用户",
    role="member",
    tenant_id=_TENANT_ID,
    is_active=True,
)


class _ScalarResult:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v


class _FakeDB:
    def __init__(self, *, policy=None):
        self._policy = policy
        self.added = []
        self.flushed = False

    async def execute(self, stmt):
        return _ScalarResult(self._policy)

    def add(self, obj):
        obj.id = _POLICY_ID
        obj.tenant_id = _TENANT_ID
        obj.version = 1
        obj.zone_guard = obj.zone_guard if hasattr(obj, "zone_guard") and obj.zone_guard else {}
        obj.egress_guard = obj.egress_guard if hasattr(obj, "egress_guard") and obj.egress_guard else {}
        self._policy = obj
        self.added.append(obj)

    async def flush(self):
        self.flushed = True


def _build_client(*, policy=None, user=None):
    app = FastAPI()
    app.include_router(gp_router)
    fake_db = _FakeDB(policy=policy)

    async def override_admin():
        return user or _ADMIN_USER

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_admin] = override_admin
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db


# ─── GET /guard-policies ────────────────────────────────


def test_get_policy_returns_existing():
    """GET returns existing Guard policy."""
    existing = SimpleNamespace(
        id=_POLICY_ID,
        tenant_id=_TENANT_ID,
        version=3,
        zone_guard={"blocked_zones": ["external"]},
        egress_guard={"allowed_domains": ["*.internal.com"]},
    )
    client, _ = _build_client(policy=existing)
    resp = client.get("/guard-policies")

    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == 3
    assert data["zone_guard"]["blocked_zones"] == ["external"]
    assert data["egress_guard"]["allowed_domains"] == ["*.internal.com"]


def test_get_policy_creates_default_when_none():
    """GET with no existing policy creates a default empty one."""
    client, fake_db = _build_client(policy=None)
    resp = client.get("/guard-policies")

    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == 1
    assert data["zone_guard"] == {}
    assert data["egress_guard"] == {}
    assert len(fake_db.added) == 1


# ─── PUT /guard-policies ────────────────────────────────


def test_update_policy_bumps_version():
    """PUT updates fields and bumps the policy version."""
    existing = SimpleNamespace(
        id=_POLICY_ID,
        tenant_id=_TENANT_ID,
        version=2,
        zone_guard={},
        egress_guard={},
    )
    client, _ = _build_client(policy=existing)
    with patch.object(gp_mod, "bump_sync_version", new_callable=AsyncMock, return_value=6):
        resp = client.put("/guard-policies", json={
            "zone_guard": {"blocked_zones": ["sandbox"]},
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == 3
    assert data["zone_guard"]["blocked_zones"] == ["sandbox"]
    assert data["egress_guard"] == {}


def test_update_policy_partial_update():
    """PUT with only egress_guard leaves zone_guard unchanged."""
    existing = SimpleNamespace(
        id=_POLICY_ID,
        tenant_id=_TENANT_ID,
        version=1,
        zone_guard={"keep": True},
        egress_guard={},
    )
    client, _ = _build_client(policy=existing)
    with patch.object(gp_mod, "bump_sync_version", new_callable=AsyncMock, return_value=3):
        resp = client.put("/guard-policies", json={
            "egress_guard": {"block_all": True},
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["zone_guard"] == {"keep": True}
    assert data["egress_guard"] == {"block_all": True}


def test_get_policy_no_tenant():
    """User without tenant gets 400."""
    no_tenant_user = SimpleNamespace(
        id=uuid4(), username="orphan", role="org_admin", tenant_id=None, is_active=True,
    )
    client, _ = _build_client(user=no_tenant_user)
    resp = client.get("/guard-policies")
    assert resp.status_code == 400
