"""Part 4 — server-first MCP extension API (route-level, Hive unit-first style).

Drives the new router through a FastAPI TestClient with dependency overrides and
monkeypatched service functions (no live DB). Asserts the routes wire to the
right service call, enforce agent access, and that DTOs carry NO ``pack`` /
``pack_name`` field. Mirrors test_agent_capability_installs_api.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.mcp_servers as mcp_mod
from app.api.mcp_servers import router
from app.core.security import get_current_admin, get_current_user
from app.database import get_db


class _FakeDB:
    async def execute(self, _stmt):
        raise AssertionError("Unexpected execute() call")


def _build_client(*, role="member"):
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB()
    current_user = SimpleNamespace(id=uuid4(), role=role, tenant_id=uuid4(), is_active=True)

    async def override_user():
        return current_user

    async def override_admin():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_current_admin] = override_admin
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db, current_user


def test_get_extensions_returns_skills_and_mcp_servers(monkeypatch):
    agent_id = uuid4()
    client, fake_db, current_user = _build_client()

    async def fake_check(db_session, user, target_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert target_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=current_user.tenant_id), "manage"

    async def fake_extensions(db_session, target_agent_id):
        assert target_agent_id == agent_id
        return {
            "skills": [
                {"id": "market-research", "name": "market-research", "source": "workspace", "status": "available"}
            ],
            "mcp_servers": [
                {
                    "id": str(uuid4()),
                    "name": "GitHub",
                    "status": "connected",
                    "enabled": True,
                    "tool_count": 18,
                    "default_tool_mode": "auto",
                    "always_load": True,
                }
            ],
        }

    monkeypatch.setattr(mcp_mod, "check_agent_access", fake_check)
    monkeypatch.setattr(mcp_mod, "get_agent_extensions", fake_extensions)

    resp = client.get(f"/agents/{agent_id}/extensions")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"skills", "mcp_servers"}
    assert "pack_name" not in body
    assert all("pack_name" not in s for s in body["mcp_servers"])


def test_enterprise_list_is_admin_and_server_first(monkeypatch):
    client, fake_db, current_user = _build_client(role="platform_admin")

    async def fake_list(db_session, tenant_id):
        assert db_session is fake_db
        assert tenant_id == current_user.tenant_id
        return [
            {
                "id": str(uuid4()),
                "name": "GitHub",
                "server_key": "github",
                "status": "connected",
                "auth_status": "configured",
                "transport": "sse",
                "tool_count": 18,
                "agent_count": 4,
                "agents": [{"id": str(uuid4()), "name": "Engineer", "enabled": True}],
            }
        ]

    monkeypatch.setattr(mcp_mod, "list_tenant_servers", fake_list)

    # Canonical path — no /records suffix.
    resp = client.get("/enterprise/mcp-servers")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert "pack_name" not in body[0]
    assert body[0]["server_key"] == "github"


def test_import_route_delegates_to_import_and_register(monkeypatch):
    client, fake_db, current_user = _build_client(role="platform_admin")
    captured = {}

    async def fake_import(db_session, tenant_id, *, server_id, mcp_url, server_name, config):
        captured.update(
            tenant_id=tenant_id, server_id=server_id, mcp_url=mcp_url, server_name=server_name, config=config
        )
        return {"message": "ok", "server": {"id": str(uuid4()), "server_key": "github"}}

    monkeypatch.setattr(mcp_mod, "import_and_register", fake_import)

    resp = client.post("/enterprise/mcp-servers/import", json={"mcp_url": "https://gh/sse", "server_name": "GitHub"})

    assert resp.status_code == 200
    assert captured["tenant_id"] == current_user.tenant_id
    assert captured["mcp_url"] == "https://gh/sse"
    assert captured["server_name"] == "GitHub"
    assert "pack_name" not in resp.json()["server"]


def test_import_route_maps_value_error_to_400(monkeypatch):
    client, _fake_db, _current_user = _build_client(role="platform_admin")

    async def fake_import(db_session, tenant_id, **kwargs):
        raise ValueError("server_id or mcp_url is required")

    monkeypatch.setattr(mcp_mod, "import_and_register", fake_import)

    resp = client.post("/enterprise/mcp-servers/import", json={})

    assert resp.status_code == 400
    assert "mcp_url is required" in resp.json()["detail"]


def test_delete_route_uses_stable_server_id(monkeypatch):
    client, fake_db, current_user = _build_client(role="platform_admin")
    server_id = uuid4()
    captured = {}

    async def fake_delete(db_session, tenant_id, target_server_id):
        captured.update(tenant_id=tenant_id, server_id=target_server_id)
        return {"status": "deleted", "server_id": str(target_server_id)}

    monkeypatch.setattr(mcp_mod, "delete_tenant_server", fake_delete)

    resp = client.delete(f"/enterprise/mcp-servers/{server_id}")

    assert resp.status_code == 200
    assert captured["tenant_id"] == current_user.tenant_id
    assert captured["server_id"] == server_id
    assert resp.json()["status"] == "deleted"


def test_delete_route_maps_missing_to_404(monkeypatch):
    client, _fake_db, _current_user = _build_client(role="platform_admin")

    async def fake_delete(db_session, tenant_id, target_server_id):
        raise ValueError("MCP server not found")

    monkeypatch.setattr(mcp_mod, "delete_tenant_server", fake_delete)

    resp = client.delete(f"/enterprise/mcp-servers/{uuid4()}")

    assert resp.status_code == 404


def test_get_agent_mcp_servers_checks_access(monkeypatch):
    agent_id = uuid4()
    client, fake_db, current_user = _build_client()
    seen = {}

    async def fake_check(db_session, user, target_agent_id):
        seen["checked"] = target_agent_id
        return SimpleNamespace(id=agent_id, tenant_id=current_user.tenant_id), "view"

    async def fake_get(db_session, target_agent_id):
        return [
            {
                "id": str(uuid4()),
                "name": "GitHub",
                "status": "connected",
                "enabled": True,
                "tool_count": 18,
                "default_tool_mode": "auto",
            }
        ]

    monkeypatch.setattr(mcp_mod, "check_agent_access", fake_check)
    monkeypatch.setattr(mcp_mod, "get_agent_mcp_servers", fake_get)

    resp = client.get(f"/agents/{agent_id}/mcp-servers")

    assert resp.status_code == 200
    assert seen["checked"] == agent_id
    assert "pack_name" not in resp.json()[0]


def test_put_agent_mcp_server_upserts_with_agent_tenant(monkeypatch):
    agent_id = uuid4()
    server_id = uuid4()
    client, fake_db, current_user = _build_client()
    agent_tenant = current_user.tenant_id
    captured = {}

    async def fake_check(db_session, user, target_agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=agent_tenant), "manage"

    async def fake_set(
        db_session, tenant_id, target_agent_id, target_server_id, *, enabled, default_tool_mode, always_load
    ):
        captured.update(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            server_id=target_server_id,
            enabled=enabled,
            default_tool_mode=default_tool_mode,
            always_load=always_load,
        )
        return {
            "id": str(uuid4()),
            "agent_id": str(target_agent_id),
            "server_id": str(target_server_id),
            "enabled": enabled,
            "default_tool_mode": default_tool_mode,
            "always_load": always_load,
        }

    monkeypatch.setattr(mcp_mod, "check_agent_access", fake_check)
    monkeypatch.setattr(mcp_mod, "set_agent_mcp_assignment", fake_set)

    resp = client.put(
        f"/agents/{agent_id}/mcp-servers/{server_id}",
        json={"enabled": True, "default_tool_mode": "approval", "always_load": True},
    )

    assert resp.status_code == 200
    assert captured["tenant_id"] == agent_tenant
    assert captured["agent_id"] == agent_id
    assert captured["server_id"] == server_id
    assert captured["enabled"] is True
    assert captured["default_tool_mode"] == "approval"
    assert captured["always_load"] is True
    assert resp.json()["enabled"] is True
    assert resp.json()["always_load"] is True


def test_put_agent_mcp_server_defaults_tool_mode_auto(monkeypatch):
    agent_id = uuid4()
    server_id = uuid4()
    client, fake_db, current_user = _build_client()
    captured = {}

    async def fake_check(db_session, user, target_agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=current_user.tenant_id), "manage"

    async def fake_set(
        db_session, tenant_id, target_agent_id, target_server_id, *, enabled, default_tool_mode, always_load
    ):
        captured["default_tool_mode"] = default_tool_mode
        captured["always_load"] = always_load
        return {
            "id": str(uuid4()),
            "agent_id": str(target_agent_id),
            "server_id": str(target_server_id),
            "enabled": enabled,
            "default_tool_mode": default_tool_mode,
            "always_load": always_load,
        }

    monkeypatch.setattr(mcp_mod, "check_agent_access", fake_check)
    monkeypatch.setattr(mcp_mod, "set_agent_mcp_assignment", fake_set)

    resp = client.put(f"/agents/{agent_id}/mcp-servers/{server_id}", json={"enabled": False})

    assert resp.status_code == 200
    assert captured["default_tool_mode"] == "auto"
    assert captured["always_load"] is False


def test_put_agent_mcp_server_rejects_invalid_default_mode():
    agent_id = uuid4()
    server_id = uuid4()
    client, _fake_db, _current_user = _build_client()

    resp = client.put(
        f"/agents/{agent_id}/mcp-servers/{server_id}",
        json={"enabled": True, "default_tool_mode": "bogus"},
    )

    assert resp.status_code == 422


def test_get_agent_mcp_server_tools_checks_access(monkeypatch):
    agent_id = uuid4()
    server_id = uuid4()
    client, fake_db, current_user = _build_client()
    seen = {}

    async def fake_check(db_session, user, target_agent_id):
        seen["checked"] = target_agent_id
        return SimpleNamespace(id=agent_id, tenant_id=current_user.tenant_id), "view"

    async def fake_list(db_session, tenant_id, target_agent_id, target_server_id):
        seen.update(tenant_id=tenant_id, server_id=target_server_id)
        return [
            {
                "tool_id": str(uuid4()),
                "tool_name": "issue_search",
                "display_name": "Issue Search",
                "mode": "deny",
                "effective_mode": "deny",
            }
        ]

    monkeypatch.setattr(mcp_mod, "check_agent_access", fake_check)
    monkeypatch.setattr(mcp_mod, "list_agent_mcp_server_tools", fake_list)

    resp = client.get(f"/agents/{agent_id}/mcp-servers/{server_id}/tools")

    assert resp.status_code == 200
    assert seen["checked"] == agent_id
    assert seen["tenant_id"] == current_user.tenant_id
    assert seen["server_id"] == server_id
    assert resp.json()[0]["effective_mode"] == "deny"


def test_put_agent_mcp_server_tool_policy_checks_access(monkeypatch):
    agent_id = uuid4()
    server_id = uuid4()
    client, fake_db, current_user = _build_client()
    seen = {}

    async def fake_check(db_session, user, target_agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=current_user.tenant_id), "manage"

    async def fake_set(db_session, tenant_id, target_agent_id, target_server_id, tool_name, *, mode):
        seen.update(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            server_id=target_server_id,
            tool_name=tool_name,
            mode=mode,
        )
        return {
            "tool_name": tool_name,
            "mode": mode,
            "effective_mode": mode,
        }

    monkeypatch.setattr(mcp_mod, "check_agent_access", fake_check)
    monkeypatch.setattr(mcp_mod, "set_agent_mcp_tool_policy", fake_set)

    resp = client.put(
        f"/agents/{agent_id}/mcp-servers/{server_id}/tools/issue_search/policy",
        json={"mode": "deny"},
    )

    assert resp.status_code == 200
    assert seen == {
        "tenant_id": current_user.tenant_id,
        "agent_id": agent_id,
        "server_id": server_id,
        "tool_name": "issue_search",
        "mode": "deny",
    }
    assert resp.json()["effective_mode"] == "deny"


def test_put_agent_mcp_server_tool_policy_rejects_invalid_mode():
    agent_id = uuid4()
    server_id = uuid4()
    client, _fake_db, _current_user = _build_client()

    resp = client.put(
        f"/agents/{agent_id}/mcp-servers/{server_id}/tools/issue_search/policy",
        json={"mode": "bogus"},
    )

    assert resp.status_code == 422


def test_backfill_route_delegates_to_service(monkeypatch):
    client, fake_db, current_user = _build_client(role="platform_admin")

    async def fake_backfill(db_session, tenant_id):
        assert db_session is fake_db
        assert tenant_id == current_user.tenant_id
        return {"tenant_id": str(tenant_id), "servers": 2, "tools": 7}

    monkeypatch.setattr(mcp_mod, "trigger_tenant_backfill", fake_backfill)

    resp = client.post("/enterprise/mcp-servers/backfill")

    assert resp.status_code == 200
    assert resp.json()["servers"] == 2
