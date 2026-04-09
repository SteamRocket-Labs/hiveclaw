"""Tests for Desktop Bootstrap & Sync endpoints (ARCHITECTURE.md §7.2)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.desktop_sync import router
from app.core.security import get_current_user
from app.database import get_db


# ─── Fixtures ───────────────────────────────────────────

_USER_ID = uuid4()
_TENANT_ID = uuid4()
_MAIN_AGENT_ID = uuid4()
_SUB_AGENT_ID = uuid4()

_FAKE_USER = SimpleNamespace(
    id=_USER_ID,
    username="zhangsan",
    email="zhangsan@test.com",
    display_name="张三",
    avatar_url=None,
    role="member",
    tenant_id=_TENANT_ID,
    department_id=None,
    is_active=True,
)

_FAKE_MAIN_AGENT = SimpleNamespace(
    id=_MAIN_AGENT_ID,
    name="张三的主 Agent",
    role_description="个人助理",
    bio=None,
    agent_kind="main",
    parent_agent_id=None,
    owner_user_id=_USER_ID,
    channel_perms=True,
    config_version=3,
    security_zone="standard",
    primary_model_id=None,
    fallback_model_id=None,
    status="running",
)

_FAKE_SUB_AGENT = SimpleNamespace(
    id=_SUB_AGENT_ID,
    name="代码助手",
    role_description="写代码",
    bio=None,
    agent_kind="sub",
    parent_agent_id=_MAIN_AGENT_ID,
    owner_user_id=_USER_ID,
    channel_perms=False,
    config_version=1,
    security_zone="standard",
    primary_model_id=None,
    fallback_model_id=None,
    status="idle",
)

_FAKE_TENANT = SimpleNamespace(id=_TENANT_ID, sync_version=5)


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return self._values


class _SingleScalar:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v


class _FakeDB:
    def __init__(self, *, agents=None, llm_models=None, tenant=None, guard_policy=None):
        self._agents = agents or []
        self._llm_models = llm_models or []
        self._tenant = tenant
        self._guard_policy = guard_policy

    async def execute(self, stmt):
        stmt_str = str(stmt)
        if "guard_policies" in stmt_str:
            return _SingleScalar(self._guard_policy)
        if "agents" in stmt_str:
            return _ScalarResult(self._agents)
        if "llm_models" in stmt_str:
            return _ScalarResult(self._llm_models)
        return _ScalarResult([])

    async def get(self, model, pk):
        if self._tenant and pk == self._tenant.id:
            return self._tenant
        return None


def _build_client(*, agents=None, llm_models=None, tenant=None):
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB(agents=agents, llm_models=llm_models, tenant=tenant)

    async def override_user():
        return _FAKE_USER

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False)


# ─── Bootstrap tests ────────────────────────────────────


def test_bootstrap_returns_full_payload():
    """Bootstrap must return sync_version, user, main_agent, sub_agents, llm_config."""
    client = _build_client(
        agents=[_FAKE_MAIN_AGENT, _FAKE_SUB_AGENT],
        tenant=_FAKE_TENANT,
    )
    resp = client.get("/desktop/bootstrap")
    assert resp.status_code == 200
    data = resp.json()

    assert data["sync_version"] == 5
    assert data["user"]["username"] == "zhangsan"
    assert len(data["agents"]) == 2
    agent_names = {a["name"] for a in data["agents"]}
    assert "张三的主 Agent" in agent_names
    assert isinstance(data["policy"], dict)
    assert isinstance(data["llm_config"], list)


def test_bootstrap_no_agents():
    """Bootstrap with no assigned agents returns null main_agent and empty sub_agents."""
    client = _build_client(agents=[], tenant=_FAKE_TENANT)
    resp = client.get("/desktop/bootstrap")
    assert resp.status_code == 200
    data = resp.json()

    assert data["agents"] == []


# ─── Sync tests ─────────────────────────────────────────


def test_sync_not_modified_when_version_matches():
    """Sync with current version returns not_modified=true."""
    client = _build_client(tenant=_FAKE_TENANT)
    resp = client.get("/desktop/sync?v=5")
    assert resp.status_code == 200
    data = resp.json()

    assert data["not_modified"] is True
    assert data["sync_version"] == 5
    assert data["agents"] is None


def test_sync_returns_changes_when_version_behind():
    """Sync with old version returns all agents and config."""
    client = _build_client(
        agents=[_FAKE_MAIN_AGENT, _FAKE_SUB_AGENT],
        tenant=_FAKE_TENANT,
    )
    resp = client.get("/desktop/sync?v=2")
    assert resp.status_code == 200
    data = resp.json()

    assert data["not_modified"] is False
    assert data["sync_version"] == 5
    assert len(data["agents"]) == 2


def test_sync_requires_version_param():
    """Sync must require the v query parameter."""
    client = _build_client(tenant=_FAKE_TENANT)
    resp = client.get("/desktop/sync")
    assert resp.status_code == 422
