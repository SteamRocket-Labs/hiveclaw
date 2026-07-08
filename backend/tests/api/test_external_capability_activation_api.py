from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.external_capabilities as external_mod
from app.api.external_capabilities import router
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    pass


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


def test_activate_external_extension_route_checks_agent_access_and_activates_selected_components(monkeypatch):
    client, fake_db, current_user = _build_client()
    agent_id = uuid4()
    snapshot_id = uuid4()

    async def fake_check(db_session, user, target_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert target_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=current_user.tenant_id), "manage"

    async def fake_activate(
        db_session,
        *,
        tenant_id,
        agent_id,
        snapshot_id,
        workspace,
        activated_by_user_id,
        component_qualified_names,
        credential_handles,
    ):
        assert db_session is fake_db
        assert tenant_id == current_user.tenant_id
        assert activated_by_user_id == current_user.id
        assert str(workspace).endswith(str(agent_id))
        assert component_qualified_names == ["docs-pack:skill:audit"]
        assert credential_handles == {"docs_api_key": "credential-handle-123"}
        return {"status": "active", "snapshot_id": str(snapshot_id)}

    monkeypatch.setattr(external_mod, "check_agent_access", fake_check)
    monkeypatch.setattr(external_mod, "activate_external_extension_for_agent", fake_activate)

    resp = client.post(
        f"/agents/{agent_id}/external-extensions/{snapshot_id}/activate",
        json={
            "component_qualified_names": ["docs-pack:skill:audit"],
            "credential_handles": {"docs_api_key": "credential-handle-123"},
        },
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
