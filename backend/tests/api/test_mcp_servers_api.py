"""Part 4 — server-first MCP extension API (route-level, Hive unit-first style).

Drives the new router through a FastAPI TestClient with dependency overrides and
monkeypatched service functions (no live DB). Asserts the routes wire to the
right service call, enforce agent access, and that DTOs carry NO ``pack`` /
``pack_name`` field. Mirrors test_agent_capability_installs_api.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
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


def test_enterprise_records_is_admin_and_server_first(monkeypatch):
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

    resp = client.get("/enterprise/mcp-servers/records")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert "pack_name" not in body[0]
    assert body[0]["server_key"] == "github"


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

    async def fake_set(db_session, tenant_id, target_agent_id, target_server_id, *, enabled, default_tool_mode):
        captured.update(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            server_id=target_server_id,
            enabled=enabled,
            default_tool_mode=default_tool_mode,
        )
        return {
            "id": str(uuid4()),
            "agent_id": str(target_agent_id),
            "server_id": str(target_server_id),
            "enabled": enabled,
            "default_tool_mode": default_tool_mode,
        }

    monkeypatch.setattr(mcp_mod, "check_agent_access", fake_check)
    monkeypatch.setattr(mcp_mod, "set_agent_mcp_assignment", fake_set)

    resp = client.put(
        f"/agents/{agent_id}/mcp-servers/{server_id}",
        json={"enabled": True, "default_tool_mode": "approval"},
    )

    assert resp.status_code == 200
    assert captured["tenant_id"] == agent_tenant
    assert captured["agent_id"] == agent_id
    assert captured["server_id"] == server_id
    assert captured["enabled"] is True
    assert captured["default_tool_mode"] == "approval"
    assert resp.json()["enabled"] is True


def test_put_agent_mcp_server_defaults_tool_mode_auto(monkeypatch):
    agent_id = uuid4()
    server_id = uuid4()
    client, fake_db, current_user = _build_client()
    captured = {}

    async def fake_check(db_session, user, target_agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=current_user.tenant_id), "manage"

    async def fake_set(db_session, tenant_id, target_agent_id, target_server_id, *, enabled, default_tool_mode):
        captured["default_tool_mode"] = default_tool_mode
        return {
            "id": str(uuid4()),
            "agent_id": str(target_agent_id),
            "server_id": str(target_server_id),
            "enabled": enabled,
            "default_tool_mode": default_tool_mode,
        }

    monkeypatch.setattr(mcp_mod, "check_agent_access", fake_check)
    monkeypatch.setattr(mcp_mod, "set_agent_mcp_assignment", fake_set)

    resp = client.put(f"/agents/{agent_id}/mcp-servers/{server_id}", json={"enabled": False})

    assert resp.status_code == 200
    assert captured["default_tool_mode"] == "auto"


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
