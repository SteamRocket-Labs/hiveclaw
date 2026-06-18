from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.admin as admin_api
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    def __init__(self):
        self.sync_session = SimpleNamespace(info={})
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return SimpleNamespace()


def _platform_admin():
    return SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4(), username="admin")


def _member():
    return SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")


def _client(user):
    app = FastAPI()
    app.include_router(admin_api.router)

    async def override_user():
        return user

    async def override_db():
        yield _FakeDB()

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_harness_canary_requires_platform_admin() -> None:
    client = _client(_member())

    resp = client.post("/admin/harness-canary/run", json={})

    assert resp.status_code == 403


def test_harness_canary_calls_service(monkeypatch) -> None:
    agent_id = uuid4()
    tenant_id = uuid4()
    captured = {}

    async def fake_run(
        *,
        db,
        tenant_id=None,
        agent_id=None,
        include_h4=True,
        include_h5=True,
        max_agents=50,
        force=False,
    ):
        captured["db"] = db
        captured["tenant_id"] = tenant_id
        captured["agent_id"] = agent_id
        captured["include_h4"] = include_h4
        captured["include_h5"] = include_h5
        captured["max_agents"] = max_agents
        captured["force"] = force
        return {
            "schema": "harness_canary_run.v1",
            "mode": "write",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "totals": {"agents_considered": 1, "agents_written": 1},
            "results": [{"agent_id": str(agent_id), "status": "written"}],
        }

    monkeypatch.setattr(admin_api, "run_harness_canary", fake_run)
    client = _client(_platform_admin())

    resp = client.post(
        "/admin/harness-canary/run",
        json={
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "include_h4": True,
            "include_h5": False,
            "max_agents": 7,
            "force": True,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["schema"] == "harness_canary_run.v1"
    assert data["mode"] == "write"
    assert captured["tenant_id"] == tenant_id
    assert captured["agent_id"] == agent_id
    assert captured["include_h4"] is True
    assert captured["include_h5"] is False
    assert captured["max_agents"] == 7
    assert captured["force"] is True
