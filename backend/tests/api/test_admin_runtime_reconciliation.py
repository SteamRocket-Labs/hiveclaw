from __future__ import annotations

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
        self.statements: list[str] = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return SimpleNamespace()


def _client(role: str = "platform_admin") -> tuple[TestClient, _FakeDB]:
    app = FastAPI()
    app.include_router(admin_api.router)
    fake_db = _FakeDB()

    async def override_user():
        return SimpleNamespace(id=uuid4(), role=role, tenant_id=uuid4(), username="admin")

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app), fake_db


def test_admin_runtime_reconciliation_requires_platform_admin() -> None:
    client, _fake_db = _client(role="org_admin")

    response = client.get("/admin/runtime-reconciliation", params={"tenant_id": str(uuid4())})

    assert response.status_code == 403


def test_admin_runtime_reconciliation_routes_delegate_to_service(monkeypatch) -> None:
    from app import database

    tenant_id = uuid4()
    task_id = uuid4()
    captured = {}

    async def fake_list(db, *, tenant_id, status, limit, agent_id=None):
        captured["list"] = {"tenant_id": tenant_id, "status": status, "limit": limit, "agent_id": agent_id}
        return [{"task_id": str(task_id), "status": "needs_reconciliation"}]

    async def fake_get(db, *, tenant_id, task_id):
        captured["get"] = {"tenant_id": tenant_id, "task_id": task_id}
        return {"task_id": str(task_id), "status": "needs_reconciliation"}

    async def fake_apply(db, *, tenant_id, task_id, action, reason, actor_user_id):
        captured["apply"] = {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "action": action,
            "reason": reason,
            "actor_user_id": actor_user_id,
        }
        return {"task_id": str(task_id), "status": "reconciled", "action": action}

    monkeypatch.setattr(admin_api, "list_runtime_reconciliation_tasks", fake_list)
    monkeypatch.setattr(admin_api, "get_runtime_reconciliation_task", fake_get)
    monkeypatch.setattr(admin_api, "apply_runtime_reconciliation_action", fake_apply)

    client, fake_db = _client()
    list_resp = client.get("/admin/runtime-reconciliation", params={"tenant_id": str(tenant_id), "limit": "25"})
    get_resp = client.get(f"/admin/runtime-reconciliation/{task_id}", params={"tenant_id": str(tenant_id)})
    action_resp = client.post(
        f"/admin/runtime-reconciliation/{task_id}/action",
        params={"tenant_id": str(tenant_id)},
        json={"action": "mark_resolved", "reason": "operator verified no duplicate side effect"},
    )

    assert list_resp.status_code == 200
    assert list_resp.json()[0]["task_id"] == str(task_id)
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "needs_reconciliation"
    assert action_resp.status_code == 200
    assert action_resp.json()["status"] == "reconciled"
    assert captured["list"]["tenant_id"] == tenant_id
    assert captured["list"]["limit"] == 25
    assert captured["apply"]["action"] == "mark_resolved"
    assert fake_db.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(tenant_id)
    assert any(f"SET LOCAL app.current_tenant_id = '{tenant_id}'" in stmt for stmt in fake_db.statements)
