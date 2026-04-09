from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

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


@pytest.mark.asyncio
async def test_get_agent_capability_installs_returns_persistent_install_state(monkeypatch):
    expected_agent_id = uuid4()
    client, fake_db, current_user = _build_client()

    async def fake_check_agent_access(db_session, user, target_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert target_agent_id == expected_agent_id
        return SimpleNamespace(id=expected_agent_id), "manage"

    async def fake_list_capability_installs(*, agent_id):
        assert agent_id == expected_agent_id
        return [
            {
                "id": str(uuid4()),
                "agent_id": str(expected_agent_id),
                "kind": "mcp_server",
                "source_key": "smithery/github",
                "normalized_key": "smithery/github",
                "display_name": "smithery/github",
                "status": "failed",
                "installed_via": "hr_agent",
                "error_code": "provider_error",
                "error_message": "OAuth required",
                "metadata": {"provider": "smithery"},
            },
        ]

    monkeypatch.setattr(agents_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(
        "app.services.capability_install_service.list_capability_installs",
        fake_list_capability_installs,
    )

    response = client.get(f"/agents/{expected_agent_id}/capability-installs")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["kind"] == "mcp_server"
    assert payload[0]["status"] == "failed"
    assert payload[0]["error_code"] == "provider_error"
