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
    pass


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


def test_autonomy_repair_plan_requires_platform_admin() -> None:
    client = _client(_member())

    resp = client.get("/admin/autonomy-repair-plan")

    assert resp.status_code == 403


def test_autonomy_repair_plan_returns_dry_run_service_report(monkeypatch) -> None:
    agent_id = uuid4()
    tenant_id = uuid4()
    captured = {}

    async def fake_report(*, db, tenant_id=None, agent_id=None, lookback_hours=24):
        captured["db"] = db
        captured["tenant_id"] = tenant_id
        captured["agent_id"] = agent_id
        captured["lookback_hours"] = lookback_hours
        return {
            "schema": "autonomy_repair_plan.v1",
            "mode": "dry_run",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "lookback_hours": lookback_hours,
            "totals": {"agents": 1, "audit_findings": 1, "actions": 1},
            "actions": [
                {
                    "action_id": "act-1",
                    "action_type": "classify_scheduled_trigger",
                    "auto_apply": True,
                    "risk": "low",
                    "agent_id": str(agent_id),
                    "trigger_id": "trigger-1",
                    "objective_id": None,
                    "focus_ref": None,
                    "summary": "Mark trigger as scheduled_job.",
                    "finding_categories": ["scheduled_trigger_without_focus_ref"],
                    "evidence": {},
                    "proposed_change": {"set_config": {"trigger_class": "scheduled_job"}},
                    "preconditions": [],
                    "manual_steps": [],
                }
            ],
            "audit_totals": {"findings": 1},
        }

    monkeypatch.setattr(admin_api, "build_autonomy_repair_plan", fake_report)
    client = _client(_platform_admin())

    resp = client.get(
        "/admin/autonomy-repair-plan",
        params={
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "lookback_hours": 12,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["schema"] == "autonomy_repair_plan.v1"
    assert data["mode"] == "dry_run"
    assert data["lookback_hours"] == 12
    assert data["actions"][0]["action_type"] == "classify_scheduled_trigger"
    assert captured["tenant_id"] == tenant_id
    assert captured["agent_id"] == agent_id
    assert captured["lookback_hours"] == 12


def test_autonomy_repair_apply_requires_platform_admin() -> None:
    client = _client(_member())

    resp = client.post("/admin/autonomy-repair-plan/apply")

    assert resp.status_code == 403


def test_autonomy_repair_apply_calls_service(monkeypatch) -> None:
    agent_id = uuid4()
    tenant_id = uuid4()
    captured = {}

    async def fake_apply(*, db, tenant_id=None, agent_id=None, lookback_hours=24, action_ids=None, max_risk="medium"):
        captured["db"] = db
        captured["tenant_id"] = tenant_id
        captured["agent_id"] = agent_id
        captured["lookback_hours"] = lookback_hours
        captured["action_ids"] = action_ids
        captured["max_risk"] = max_risk
        return {
            "schema": "autonomy_repair_apply.v1",
            "mode": "apply",
            "lookback_hours": lookback_hours,
            "totals": {"planned": 2, "applied": 1, "skipped": 1, "failed": 0},
            "results": [{"action_id": "act-1", "status": "applied"}],
            "post_apply_plan": {"totals": {"actions": 1}},
        }

    monkeypatch.setattr(admin_api, "apply_autonomy_repair_plan", fake_apply)
    client = _client(_platform_admin())

    resp = client.post(
        "/admin/autonomy-repair-plan/apply",
        params={
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "lookback_hours": 12,
        },
        json={"action_ids": ["act-1"], "max_risk": "low"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["schema"] == "autonomy_repair_apply.v1"
    assert data["totals"]["applied"] == 1
    assert captured["tenant_id"] == tenant_id
    assert captured["agent_id"] == agent_id
    assert captured["lookback_hours"] == 12
    assert captured["action_ids"] == ["act-1"]
    assert captured["max_risk"] == "low"
