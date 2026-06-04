from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.admin as admin_api
from app.core.security import get_current_user


class _StubWorkflowOpsService:
    def __init__(self, *_, **__):
        pass

    async def inspect_run(self, run_id, *, tenant_id):
        return {"run_id": str(run_id), "tenant_id": str(tenant_id), "status": "completed", "steps": []}

    async def export_journal(self, run_id, *, tenant_id):
        return {"run": {"run_id": str(run_id), "tenant_id": str(tenant_id)}, "steps": [], "leaf_calls": []}

    async def cancel_run(self, run_id, *, tenant_id, reason):
        return {"run_id": str(run_id), "tenant_id": str(tenant_id), "status": "killed", "reason": reason}

    async def force_suspend_run(self, run_id, *, tenant_id, reason):
        return {"run_id": str(run_id), "tenant_id": str(tenant_id), "status": "suspended", "reason": reason}

    async def replay_from_step(self, run_id, *, tenant_id, step_id, reason):
        if step_id == "conflict":
            raise admin_api.WorkflowOpsConflict("run is not quiescent")
        return {
            "run_id": str(run_id),
            "tenant_id": str(tenant_id),
            "status": "running",
            "replay_from_step": step_id,
            "reason": reason,
        }


def _client(role: str = "platform_admin") -> TestClient:
    app = FastAPI()
    app.include_router(admin_api.router)

    async def override_user():
        return SimpleNamespace(id=uuid4(), role=role, tenant_id=uuid4(), username="admin")

    app.dependency_overrides[get_current_user] = override_user
    return TestClient(app)


def test_admin_workflow_ops_require_platform_admin(monkeypatch) -> None:
    monkeypatch.setattr(admin_api, "WorkflowOpsService", _StubWorkflowOpsService)
    client = _client(role="member")
    run_id = uuid4()
    tenant_id = uuid4()

    resp = client.get(f"/admin/workflows/{run_id}", params={"tenant_id": str(tenant_id)})

    assert resp.status_code == 403


def test_admin_workflow_ops_routes_delegate_to_service(monkeypatch) -> None:
    monkeypatch.setattr(admin_api, "WorkflowOpsService", _StubWorkflowOpsService)
    client = _client()
    run_id = uuid4()
    tenant_id = uuid4()

    inspect_resp = client.get(f"/admin/workflows/{run_id}", params={"tenant_id": str(tenant_id)})
    export_resp = client.get(f"/admin/workflows/{run_id}/journal", params={"tenant_id": str(tenant_id)})
    cancel_resp = client.post(
        f"/admin/workflows/{run_id}/cancel",
        params={"tenant_id": str(tenant_id)},
        json={"reason": "bad run"},
    )
    suspend_resp = client.post(
        f"/admin/workflows/{run_id}/force-suspend",
        params={"tenant_id": str(tenant_id)},
        json={"reason": "manual audit"},
    )
    replay_resp = client.post(
        f"/admin/workflows/{run_id}/replay-from-step",
        params={"tenant_id": str(tenant_id)},
        json={"step_id": "write", "reason": "bad draft"},
    )
    conflict_resp = client.post(
        f"/admin/workflows/{run_id}/replay-from-step",
        params={"tenant_id": str(tenant_id)},
        json={"step_id": "conflict", "reason": "still running"},
    )

    assert inspect_resp.status_code == 200
    assert inspect_resp.json()["status"] == "completed"
    assert export_resp.status_code == 200
    assert export_resp.json()["run"]["run_id"] == str(run_id)
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "killed"
    assert suspend_resp.status_code == 200
    assert suspend_resp.json()["status"] == "suspended"
    assert replay_resp.status_code == 200
    assert replay_resp.json()["replay_from_step"] == "write"
    assert conflict_resp.status_code == 409
    assert "quiescent" in conflict_resp.json()["detail"]
