from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.a2a as a2a_mod
from app.api.a2a import router as a2a_router
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    async def execute(self, _stmt):
        raise AssertionError("Unexpected execute() call")


def _client():
    app = FastAPI()
    app.include_router(a2a_router)
    current_user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), is_active=True)
    fake_db = _FakeDB()

    async def override_user():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db, current_user


def test_get_a2a_collaborators_uses_canonical_read_model(monkeypatch):
    client, fake_db, current_user = _client()
    agent_id = uuid4()

    async def fake_check_agent_access(db_session, user, target_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert target_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "use"

    async def fake_read_model(db_session, target_agent_id):
        assert db_session is fake_db
        assert target_agent_id == agent_id
        return {
            "agent_id": str(agent_id),
            "same_owner_agents": [{"id": str(uuid4()), "name": "Same Owner", "relation": "same_owner"}],
            "public_agents": [{"id": str(uuid4()), "name": "Public Agent", "relation": "public_agent"}],
            "collaboration_groups": [
                {
                    "group_id": str(uuid4()),
                    "group_name": "Research Pod",
                    "members": [{"id": str(uuid4()), "name": "Group Peer", "relation": "group_member"}],
                }
            ],
        }

    monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(a2a_mod, "build_a2a_collaboration_read_model", fake_read_model)

    response = client.get(f"/agents/{agent_id}/a2a/collaborators")

    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == str(agent_id)
    assert body["same_owner_agents"][0]["name"] == "Same Owner"
    assert body["public_agents"][0]["name"] == "Public Agent"
    assert body["collaboration_groups"][0]["members"][0]["name"] == "Group Peer"
