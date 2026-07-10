"""Tests for Desktop Agent CRUD endpoints (ARCHITECTURE.md §7.3)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

import app.api.desktop_agents as agents_mod
from app.api.desktop_agents import router
from app.core.security import get_current_user
from app.database import get_db


# ─── Fixtures ───────────────────────────────────────────

_USER_ID = uuid4()
_OTHER_USER_ID = uuid4()
_TENANT_ID = uuid4()
_MAIN_AGENT_ID = uuid4()
_SUB_AGENT_ID = uuid4()

_FAKE_USER = SimpleNamespace(
    id=_USER_ID,
    username="zhangsan",
    email="zhangsan@test.com",
    display_name="张三",
    role="member",
    tenant_id=_TENANT_ID,
    is_active=True,
)
_FAKE_ADMIN_USER = SimpleNamespace(
    id=_USER_ID,
    username="admin",
    email="admin@test.com",
    display_name="管理员",
    role="org_admin",
    tenant_id=_TENANT_ID,
    is_active=True,
)

_FAKE_MAIN_AGENT = SimpleNamespace(
    id=_MAIN_AGENT_ID,
    name="主Agent",
    role_description="助理",
    bio=None,
    agent_kind="main",
    parent_agent_id=None,
    owner_user_id=_USER_ID,
    channel_perms=True,
    config_version=1,
    security_zone="standard",
    creator_id=_USER_ID,
    tenant_id=_TENANT_ID,
    status="running",
)


class _ScalarResult:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v


class _FakeDB:
    def __init__(self, *, main_agent=None, agents_by_id=None):
        self._main_agent = main_agent
        self._agents_by_id = agents_by_id or {}
        self.added = []
        self.deleted = []
        self.flushed = False
        self.bump_called = False

    async def execute(self, stmt):
        statement_text = str(stmt)
        if "FROM ai_asset_records" in statement_text or "FROM config_revisions" in statement_text:
            return _ScalarResult(None)
        # For the main agent query
        return _ScalarResult(self._main_agent)

    async def get(self, model, pk):
        return self._agents_by_id.get(pk)

    def add(self, obj):
        # Simulate DB assignment for flush
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid4()
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def delete(self, obj):
        self.deleted.append(obj)


def _build_client(*, main_agent=None, agents_by_id=None, user=None):
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB(main_agent=main_agent, agents_by_id=agents_by_id)

    async def override_user():
        return user or _FAKE_USER

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db


# ─── POST /desktop/agents (create sub-agent) ───────────


def test_create_sub_agent_success():
    """Creating a sub-agent under the user's main agent must succeed."""
    client, fake_db = _build_client(main_agent=_FAKE_MAIN_AGENT)
    with (
        patch.object(agents_mod, "bump_sync_version", new_callable=AsyncMock, return_value=2),
        patch.object(agents_mod, "ensure_main_agent", new_callable=AsyncMock, return_value=_FAKE_MAIN_AGENT),
    ):
        resp = client.post("/desktop/agents", json={
            "name": "代码助手",
            "role_description": "写代码",
            "execution_mode": "coordinator",
            "smart_model_routing": {"enabled": True, "max_simple_chars": 120, "max_simple_words": 18},
        })

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "代码助手"
    assert data["execution_mode"] == "coordinator"
    assert data["smart_model_routing"] == {"enabled": True, "max_simple_chars": 120, "max_simple_words": 18}
    assert len([obj for obj in fake_db.added if obj.__class__.__name__ in {"Agent", "KnowledgeGrant"}]) == 2
    agent = next(obj for obj in fake_db.added if obj.__class__.__name__ == "Agent")
    grant = next(obj for obj in fake_db.added if obj.__class__.__name__ == "KnowledgeGrant")
    assert agent.parent_agent_id == _MAIN_AGENT_ID
    assert grant.scope_type == "person"
    assert grant.scope_id == _USER_ID
    assert grant.grantee_type == "agent"
    assert grant.grantee_id == agent.id


def test_create_agent_succeeds_without_existing_agents():
    """Creating an agent works even without any prior agents."""
    client, fake_db = _build_client(main_agent=None)
    auto_main = SimpleNamespace(id=_MAIN_AGENT_ID)
    with (
        patch.object(agents_mod, "bump_sync_version", new_callable=AsyncMock, return_value=2),
        patch.object(agents_mod, "ensure_main_agent", new_callable=AsyncMock, return_value=auto_main),
    ):
        resp = client.post("/desktop/agents", json={"name": "测试", "role_description": "test"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "测试"
    agent = next(obj for obj in fake_db.added if obj.__class__.__name__ == "Agent")
    assert agent.parent_agent_id == _MAIN_AGENT_ID


# ─── PATCH /desktop/agents/{id} (update sub-agent) ─────


def test_update_own_sub_agent():
    """User can update their own sub-agent."""
    sub = SimpleNamespace(
        id=_SUB_AGENT_ID,
        name="旧名",
        role_description="旧描述",
        bio=None,
        agent_kind="sub",
        parent_agent_id=_MAIN_AGENT_ID,
        owner_user_id=_USER_ID,
        config_version=1,
        security_zone="standard",
        execution_mode="standard",
        smart_model_routing=None,
    )
    client, _ = _build_client(agents_by_id={_SUB_AGENT_ID: sub})
    with patch.object(agents_mod, "bump_sync_version", new_callable=AsyncMock, return_value=3):
        resp = client.patch(
            f"/desktop/agents/{_SUB_AGENT_ID}",
            json={
                "name": "新名",
                "execution_mode": "coordinator",
                "smart_model_routing": {"enabled": True, "max_simple_chars": 96, "max_simple_words": 16},
            },
        )

    assert resp.status_code == 200
    assert resp.json()["name"] == "新名"
    assert resp.json()["execution_mode"] == "coordinator"
    assert resp.json()["smart_model_routing"] == {"enabled": True, "max_simple_chars": 96, "max_simple_words": 16}
    assert sub.config_version == 2


def test_update_other_users_agent_forbidden():
    """Cannot update another user's agent."""
    other_agent = SimpleNamespace(
        id=_SUB_AGENT_ID,
        name="别人的",
        agent_kind="sub",
        owner_user_id=_OTHER_USER_ID,
    )
    client, _ = _build_client(agents_by_id={_SUB_AGENT_ID: other_agent})
    resp = client.patch(f"/desktop/agents/{_SUB_AGENT_ID}", json={"name": "劫持"})
    assert resp.status_code == 403


def test_update_other_users_agent_returns_403():
    """Cannot modify another user's agent."""
    other_agent = SimpleNamespace(
        id=_MAIN_AGENT_ID,
        name="主Agent",
        owner_user_id=_OTHER_USER_ID,
    )
    client, _ = _build_client(agents_by_id={_MAIN_AGENT_ID: other_agent})
    resp = client.patch(f"/desktop/agents/{_MAIN_AGENT_ID}", json={"name": "改主Agent"})
    assert resp.status_code == 403


def test_update_own_main_agent_forbidden():
    """Desktop must not modify the user's root agent."""
    own_main_agent = SimpleNamespace(
        id=_MAIN_AGENT_ID,
        name="主Agent",
        owner_user_id=_USER_ID,
        parent_agent_id=None,
    )
    client, _ = _build_client(agents_by_id={_MAIN_AGENT_ID: own_main_agent})
    resp = client.patch(f"/desktop/agents/{_MAIN_AGENT_ID}", json={"name": "改主Agent"})
    assert resp.status_code == 403


# ─── DELETE /desktop/agents/{id} ────────────────────────


def test_member_cannot_delete_own_sub_agent():
    """Sub-agent is an enterprise asset; members cannot delete even their own sub-agent."""
    sub = SimpleNamespace(
        id=_SUB_AGENT_ID,
        name="要删的",
        agent_kind="sub",
        owner_user_id=_USER_ID,
        parent_agent_id=_MAIN_AGENT_ID,
        deleted_at=None,
        deactivated_at=None,
    )
    client, fake_db = _build_client(agents_by_id={_SUB_AGENT_ID: sub})
    with patch.object(agents_mod, "soft_delete_agent", new_callable=AsyncMock) as soft_delete:
        resp = client.delete(f"/desktop/agents/{_SUB_AGENT_ID}")

    assert resp.status_code == 403
    assert fake_db.deleted == []
    soft_delete.assert_not_awaited()


def test_admin_can_delete_own_sub_agent():
    """Admin can soft-delete a desktop sub-agent asset."""
    sub = SimpleNamespace(
        id=_SUB_AGENT_ID,
        name="要删的",
        agent_kind="sub",
        owner_user_id=_USER_ID,
        parent_agent_id=_MAIN_AGENT_ID,
        deleted_at=None,
        deactivated_at=None,
    )
    client, fake_db = _build_client(agents_by_id={_SUB_AGENT_ID: sub}, user=_FAKE_ADMIN_USER)
    with (
        patch.object(agents_mod, "bump_sync_version", new_callable=AsyncMock, return_value=4),
        patch.object(agents_mod, "soft_delete_agent", new_callable=AsyncMock) as soft_delete,
    ):
        resp = client.delete(f"/desktop/agents/{_SUB_AGENT_ID}")

    assert resp.status_code == 204
    assert fake_db.deleted == []
    soft_delete.assert_awaited_once_with(fake_db, sub, actor_id=_USER_ID, reason="desktop_delete_sub_agent")


def test_admin_can_delete_same_tenant_sub_agent_owned_by_another_user():
    """Admins govern tenant assets, not only their own desktop-owned sub-agents."""
    sub = SimpleNamespace(
        id=_SUB_AGENT_ID,
        name="同租户其他员工的 Sub-Agent",
        agent_kind="sub",
        owner_user_id=_OTHER_USER_ID,
        parent_agent_id=_MAIN_AGENT_ID,
        tenant_id=_TENANT_ID,
        deleted_at=None,
        deactivated_at=None,
    )
    client, fake_db = _build_client(agents_by_id={_SUB_AGENT_ID: sub}, user=_FAKE_ADMIN_USER)
    with (
        patch.object(agents_mod, "bump_sync_version", new_callable=AsyncMock, return_value=4),
        patch.object(agents_mod, "soft_delete_agent", new_callable=AsyncMock) as soft_delete,
    ):
        resp = client.delete(f"/desktop/agents/{_SUB_AGENT_ID}")

    assert resp.status_code == 204
    assert fake_db.deleted == []
    soft_delete.assert_awaited_once_with(fake_db, sub, actor_id=_USER_ID, reason="desktop_delete_sub_agent")


def test_org_admin_cannot_delete_cross_tenant_sub_agent_even_when_owner_matches():
    """Org admins must not use the desktop delete path across tenant boundaries."""
    sub = SimpleNamespace(
        id=_SUB_AGENT_ID,
        name="其他租户 Sub-Agent",
        agent_kind="sub",
        owner_user_id=_USER_ID,
        parent_agent_id=_MAIN_AGENT_ID,
        tenant_id=uuid4(),
        deleted_at=None,
        deactivated_at=None,
    )
    client, fake_db = _build_client(agents_by_id={_SUB_AGENT_ID: sub}, user=_FAKE_ADMIN_USER)
    with patch.object(agents_mod, "soft_delete_agent", new_callable=AsyncMock) as soft_delete:
        resp = client.delete(f"/desktop/agents/{_SUB_AGENT_ID}")

    assert resp.status_code == 404
    assert fake_db.deleted == []
    soft_delete.assert_not_awaited()


def test_deleted_sub_agent_is_not_editable():
    """Soft-deleted agents are hidden from Desktop mutation paths."""
    sub = SimpleNamespace(
        id=_SUB_AGENT_ID,
        name="删过的",
        agent_kind="sub",
        owner_user_id=_USER_ID,
        parent_agent_id=_MAIN_AGENT_ID,
        deleted_at=object(),
        deactivated_at=object(),
    )
    client, _ = _build_client(agents_by_id={_SUB_AGENT_ID: sub})

    resp = client.patch(f"/desktop/agents/{_SUB_AGENT_ID}", json={"name": "复活"})

    assert resp.status_code == 404


def test_member_delete_nonexistent_agent_forbidden_before_lookup():
    """Members cannot use delete to probe whether an enterprise asset exists."""
    client, _ = _build_client()
    resp = client.delete(f"/desktop/agents/{uuid4()}")
    assert resp.status_code == 403


def test_admin_delete_nonexistent_agent_returns_404():
    """Admins get a normal 404 after passing the enterprise delete gate."""
    client, _ = _build_client(user=_FAKE_ADMIN_USER)
    resp = client.delete(f"/desktop/agents/{uuid4()}")
    assert resp.status_code == 404


def test_delete_own_main_agent_forbidden():
    """Desktop must not delete the user's root agent."""
    own_main_agent = SimpleNamespace(
        id=_MAIN_AGENT_ID,
        name="主Agent",
        owner_user_id=_USER_ID,
        parent_agent_id=None,
    )
    client, _ = _build_client(agents_by_id={_MAIN_AGENT_ID: own_main_agent})
    resp = client.delete(f"/desktop/agents/{_MAIN_AGENT_ID}")
    assert resp.status_code == 403
