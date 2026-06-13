from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.agents as agents_mod
from app.api.agents import router as agents_router
from app.api.interoperability import router as interoperability_router
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    async def execute(self, _stmt):
        raise AssertionError("Unexpected execute() call")


def _build_agent_client():
    app = FastAPI()
    app.include_router(agents_router)
    current_user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), is_active=True)
    fake_db = _FakeDB()

    async def override_user():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db, current_user


def test_agent_a2a_card_route_enforces_agent_access_and_returns_card(monkeypatch):
    client, fake_db, current_user = _build_agent_client()
    agent_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Research Analyst",
        role_description="Investigates evidence.",
        bio=None,
        tenant_id=current_user.tenant_id,
        participant_id=uuid4(),
        sponsor_user_id=current_user.id,
        security_zone="standard",
        agent_class="internal_tenant",
        status="running",
        deleted_at=None,
    )

    async def fake_check(db_session, user, target_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert target_agent_id == agent_id
        return agent, "use"

    monkeypatch.setattr(agents_mod, "check_agent_access", fake_check)

    resp = client.get(f"/agents/{agent_id}/a2a-card", headers={"host": "hive.example"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["protocol"] == "a2a"
    assert body["hive"]["agent_id"] == str(agent_id)
    assert body["url"].endswith(f"/api/v1/agents/{agent_id}/a2a-card")
    assert body["authentication"]["oauth_delegation"]["status"] == "not_exposed"


def test_interoperability_profile_route_is_mounted_in_main_source() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    main_source = (root / "app" / "main.py").read_text(encoding="utf-8")

    assert "interoperability_router" in main_source
    assert "app.api.interoperability" in main_source


def test_interoperability_profile_route_returns_machine_readable_profile() -> None:
    app = FastAPI()
    app.include_router(interoperability_router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/interoperability/profile", headers={"host": "hive.example"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["platform"] == "hive"
    assert body["standards"]["mcp"]["token_passthrough"] == "forbidden"
    assert body["standards"]["a2a"]["agent_card_endpoint"] == "/api/v1/agents/{agent_id}/a2a-card"
