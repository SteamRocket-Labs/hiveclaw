"""§9 P4 red tests: workflow REST surface — preview / start / get / cancel.

API-layer responsibilities only (service behaviour is covered on real PG in
tests/services/): agent access control, risk-graded confirmation
(low → user-confirmed start allowed; high → confirmed plan REQUIRED,
hash-bound), and error mapping. The runtime service is stubbed.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.workflows as workflows_api
from app.core.security import get_current_user
from app.database import get_db
from app.runtime.workflow_engine import WorkflowRunOutcome
from app.services.workflow_runtime_service import WorkflowRunHandle


def _user(tenant_id=None):
    return SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant_id or uuid.uuid4(), username="u")


def _low_risk_definition() -> dict:
    return {
        "name": "read-probe",
        "args_schema": {},
        "steps": [
            {
                "id": "scan",
                "type": "agent_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "task": "Scan the workspace",
            }
        ],
    }


def _high_risk_definition() -> dict:
    return {
        "name": "external-send",
        "args_schema": {},
        "steps": [
            {"id": "gate", "type": "gate_step", "reason": "external send"},
            {
                "id": "send",
                "type": "agent_step",
                "leaf": {"name": "sender", "type": "worker"},
                "task": "Send externally",
                "effects": "external",
            },
        ],
    }


def _client(user, monkeypatch, *, gate_allowed=True, gate_reason=None):
    api = FastAPI()
    api.include_router(workflows_api.router)

    async def override_user():
        return user

    async def override_db():
        yield SimpleNamespace()

    api.dependency_overrides[get_current_user] = override_user
    api.dependency_overrides[get_db] = override_db

    # Agent access always passes (cross-agent denial tested separately).
    async def fake_access(db, current_user, agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=user.tenant_id, name="agent")

    monkeypatch.setattr(workflows_api, "check_agent_access", fake_access)

    async def fake_launch(**kwargs):
        fake_launch.calls.append(kwargs)
        return WorkflowRunHandle(run_id=uuid.uuid4(), outcome=WorkflowRunOutcome(status="completed"))

    fake_launch.calls = []
    monkeypatch.setattr(workflows_api, "start_ephemeral_workflow_for_agent", fake_launch)

    async def fake_gate_check(db, **kwargs):
        fake_gate_check.calls.append(kwargs)
        return SimpleNamespace(allowed=gate_allowed, reason=gate_reason)

    fake_gate_check.calls = []
    monkeypatch.setattr(workflows_api, "_plan_gate_check", fake_gate_check)

    client = TestClient(api)
    client.fake_launch = fake_launch
    client.fake_gate_check = fake_gate_check
    return client


def test_preview_returns_hash_risk_and_planned_leaves(monkeypatch):
    client = _client(_user(), monkeypatch)
    resp = client.post(
        f"/agents/{uuid.uuid4()}/workflows/preview",
        json={"definition": _low_risk_definition(), "args": {}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["definition_hash"]
    assert body["risk"] == "low"
    assert body["planned_leaf_calls"] == 1


def test_preview_maps_compile_error_to_400(monkeypatch):
    client = _client(_user(), monkeypatch)
    resp = client.post(
        f"/agents/{uuid.uuid4()}/workflows/preview",
        json={"definition": {"steps": []}, "args": {}},
    )
    assert resp.status_code == 400


def test_low_risk_start_runs_without_plan(monkeypatch):
    client = _client(_user(), monkeypatch)
    resp = client.post(
        f"/agents/{uuid.uuid4()}/workflows/runs",
        json={"definition": _low_risk_definition(), "args": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert len(client.fake_launch.calls) == 1
    assert client.fake_gate_check.calls == []  # low risk never consults the plan gate


def test_high_risk_start_without_plan_fails_closed(monkeypatch):
    client = _client(_user(), monkeypatch, gate_allowed=False, gate_reason="needs_plan")
    resp = client.post(
        f"/agents/{uuid.uuid4()}/workflows/runs",
        json={"definition": _high_risk_definition(), "args": {}},
    )
    assert resp.status_code == 409
    assert client.fake_launch.calls == []  # the run must NOT start


def test_high_risk_start_with_confirmed_plan_passes_gate(monkeypatch):
    client = _client(_user(), monkeypatch, gate_allowed=True)
    plan_id = str(uuid.uuid4())
    resp = client.post(
        f"/agents/{uuid.uuid4()}/workflows/runs",
        json={
            "definition": _high_risk_definition(),
            "args": {},
            "confirmed_plan_id": plan_id,
            "plan_version": 2,
            "plan_hash": "abc123",
        },
    )
    assert resp.status_code == 200
    assert len(client.fake_gate_check.calls) == 1
    gate_kwargs = client.fake_gate_check.calls[0]
    assert gate_kwargs["confirmed_plan_id"] == plan_id
    assert gate_kwargs["plan_version"] == 2
    assert gate_kwargs["plan_hash"] == "abc123"
    assert gate_kwargs["action_artifact"]["args_hash"]
    assert len(client.fake_launch.calls) == 1


def test_get_run_returns_steps(monkeypatch):
    client = _client(_user(), monkeypatch)
    run_id = uuid.uuid4()

    async def fake_load(self, rid, *, tenant_id=None):
        return SimpleNamespace(
            task=SimpleNamespace(
                id=run_id, status="completed", task_type="workflow", metadata_json={"definition_hash": "h"}
            ),
            steps=[SimpleNamespace(step_id="scan", status="done", step_type="agent_step", error=None)],
        )

    monkeypatch.setattr(workflows_api.WorkflowRuntimeService, "load_run", fake_load)
    resp = client.get(f"/agents/{uuid.uuid4()}/workflows/runs/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["steps"][0]["step_id"] == "scan"


def test_cancel_run_kills(monkeypatch):
    client = _client(_user(), monkeypatch)
    killed: list = []

    async def fake_kill(self, rid, *, tenant_id=None):
        killed.append(rid)

    monkeypatch.setattr(workflows_api.WorkflowRuntimeService, "kill_run", fake_kill)
    run_id = uuid.uuid4()
    resp = client.post(f"/agents/{uuid.uuid4()}/workflows/runs/{run_id}/cancel")
    assert resp.status_code == 200
    assert killed == [run_id]
