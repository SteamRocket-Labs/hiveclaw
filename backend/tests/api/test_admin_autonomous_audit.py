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


def test_autonomous_audit_requires_platform_admin() -> None:
    client = _client(_member())

    resp = client.get("/admin/autonomous-audit")

    assert resp.status_code == 403


def test_autonomous_audit_returns_service_report(monkeypatch) -> None:
    agent_id = uuid4()
    tenant_id = uuid4()
    captured = {}

    async def fake_report(*, db, tenant_id=None, agent_id=None, lookback_hours=24):
        captured["db"] = db
        captured["tenant_id"] = tenant_id
        captured["agent_id"] = agent_id
        captured["lookback_hours"] = lookback_hours
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "lookback_hours": lookback_hours,
            "totals": {"agents": 1, "findings": 1, "errors": 1, "warnings": 0, "infos": 0},
            "findings": [
                {
                    "severity": "error",
                    "category": "trigger_runtime_gap",
                    "agent_id": str(agent_id),
                    "trigger_id": None,
                    "message": "Missing trigger",
                    "evidence": {},
                    "recommendation": "Create a trigger",
                }
            ],
            "agents": [],
        }

    monkeypatch.setattr(admin_api, "build_autonomous_audit_report", fake_report)
    client = _client(_platform_admin())

    resp = client.get(
        "/admin/autonomous-audit",
        params={
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "lookback_hours": 12,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["lookback_hours"] == 12
    assert data["totals"]["findings"] == 1
    assert data["findings"][0]["category"] == "trigger_runtime_gap"
    assert captured["tenant_id"] == tenant_id
    assert captured["agent_id"] == agent_id
    assert captured["lookback_hours"] == 12
