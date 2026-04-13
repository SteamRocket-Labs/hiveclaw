from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.agents as agents_mod
from app.api.agents import router
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    async def execute(self, _stmt):
        raise AssertionError("Unexpected execute() call")


def _build_client():
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB()
    current_user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), is_active=True)

    async def override_user():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db, current_user


def test_get_agent_channel_capabilities(monkeypatch):
    expected_agent_id = uuid4()
    client, fake_db, current_user = _build_client()

    async def fake_check_agent_access(db_session, user, target_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert target_agent_id == expected_agent_id
        return SimpleNamespace(id=expected_agent_id), "manage"

    async def fake_resolve(*, db, agent_id):
        assert db is fake_db
        assert agent_id == expected_agent_id
        return [
            {
                "channel": "telegram",
                "connected": True,
                "official_api": True,
                "capabilities": {
                    "live_text": True,
                    "inbound_file": True,
                    "outbound_file": True,
                    "deferred_text": True,
                    "deferred_file": True,
                    "on_message_current_sender": True,
                    "on_message_by_name": False,
                },
                "limitations": [],
            },
        ]

    monkeypatch.setattr(agents_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr("app.services.channel_delivery_service.ChannelDeliveryService.resolve_agent_capabilities", fake_resolve)

    response = client.get(f"/agents/{expected_agent_id}/channel-capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["channel"] == "telegram"
    assert payload[0]["capabilities"]["deferred_file"] is True
