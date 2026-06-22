from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.local_bridge as local_bridge_api
from app.core.security import get_current_user
from app.database import get_db
from app.services.local_bridge_service import BridgeAuthContext


class _FakeDB:
    pass


def _user(role: str = "member"):
    return SimpleNamespace(id=uuid4(), role=role, tenant_id=uuid4(), username="member")


def _client(user=None, db=None):
    app = FastAPI()
    app.include_router(local_bridge_api.router)
    user = user or _user()

    async def override_user():
        return user

    async def override_db():
        yield db or _FakeDB()

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app), user


def test_pairing_init_returns_device_flow_payload(monkeypatch) -> None:
    captured = {}

    async def fake_create_pairing_session(db, request, *, base_url):
        captured["request"] = request
        captured["base_url"] = base_url
        return {
            "device_code": "dev_secret",
            "user_code": "HIVE-ABCD",
            "verification_uri": "http://testserver/local-bridge/activate",
            "verification_uri_complete": "http://testserver/local-bridge/activate?user_code=HIVE-ABCD",
            "expires_in": 900,
            "interval": 3,
            "pairing_id": str(uuid4()),
        }

    monkeypatch.setattr(local_bridge_api.bridge_service, "create_pairing_session", fake_create_pairing_session)
    client, _ = _client()

    resp = client.post(
        "/local-bridge/pairing/init",
        json={
            "device_name": "Rocky's MacBook",
            "client_kind": "codex",
            "device_fingerprint": "fp-1",
            "scopes": ["gateway:poll", "files:upload"],
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["device_code"] == "dev_secret"
    assert body["user_code"] == "HIVE-ABCD"
    assert body["verification_uri_complete"].endswith("user_code=HIVE-ABCD")
    assert captured["request"].device_name == "Rocky's MacBook"
    assert captured["request"].client_kind == "codex"
    assert captured["request"].device_fingerprint == "fp-1"


def test_approve_pairing_binds_current_user_agent_and_tenant(monkeypatch) -> None:
    agent_id = uuid4()
    tenant_id = uuid4()
    current_user = _user()
    current_user.tenant_id = tenant_id
    captured = {}

    async def fake_check_agent_access(db, user, requested_agent_id):
        assert user is current_user
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    async def fake_approve_pairing(db, *, user_code, user_id, tenant_id, agent_id, metadata):
        captured.update(
            {
                "user_code": user_code,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "metadata": metadata,
            }
        )
        return {
            "status": "approved",
            "pairing_id": str(uuid4()),
            "agent_id": str(agent_id),
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
        }

    monkeypatch.setattr(local_bridge_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(local_bridge_api.bridge_service, "approve_pairing_session", fake_approve_pairing)
    client, _ = _client(user=current_user)

    resp = client.post(f"/agents/{agent_id}/local-bridge/pairings/HIVE-ABCD/approve")

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert captured["user_code"] == "HIVE-ABCD"
    assert captured["user_id"] == current_user.id
    assert captured["tenant_id"] == tenant_id
    assert captured["agent_id"] == agent_id
    assert captured["metadata"]["approval_surface"] == "local_agent_link_card"


def test_approve_pairing_requires_manage_access(monkeypatch) -> None:
    agent_id = uuid4()

    async def fake_check_agent_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "use"

    async def should_not_approve(*_args, **_kwargs):
        raise AssertionError("approve must not run without manage access")

    monkeypatch.setattr(local_bridge_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(local_bridge_api.bridge_service, "approve_pairing_session", should_not_approve)
    client, _ = _client()

    resp = client.post(f"/agents/{agent_id}/local-bridge/pairings/HIVE-ABCD/approve")

    assert resp.status_code == 403


def test_pairing_exchange_returns_token_only_after_approval(monkeypatch) -> None:
    device_code = "dev_secret_value"

    async def fake_exchange_pairing_session(db, *, device_code):
        assert device_code == "dev_secret_value"
        return {
            "status": "active",
            "access_token": "hb_secret",
            "token_type": "Bearer",
            "agent_id": str(uuid4()),
            "tenant_id": str(uuid4()),
            "connection_id": str(uuid4()),
        }

    monkeypatch.setattr(local_bridge_api.bridge_service, "exchange_pairing_session", fake_exchange_pairing_session)
    client, _ = _client()

    resp = client.post("/local-bridge/pairing/exchange", json={"device_code": device_code})

    assert resp.status_code == 200
    assert resp.json()["access_token"] == "hb_secret"
    assert resp.json()["token_type"] == "Bearer"


def test_bridge_status_uses_bearer_context_from_dependency() -> None:
    connection_id = uuid4()
    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    context = BridgeAuthContext(
        connection_id=connection_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        scopes=("gateway:poll", "files:upload"),
        client_kind="generic_mcp_stdio",
        device_name="Workstation",
    )
    app = FastAPI()
    app.include_router(local_bridge_api.router)
    app.dependency_overrides[local_bridge_api.get_bridge_auth_context] = lambda: context
    client = TestClient(app)

    resp = client.get("/local-bridge/status", headers={"Authorization": "Bearer hb_secret"})

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "connected",
        "connection_id": str(connection_id),
        "tenant_id": str(tenant_id),
        "agent_id": str(agent_id),
        "user_id": str(user_id),
        "client_kind": "generic_mcp_stdio",
        "device_name": "Workstation",
        "scopes": ["gateway:poll", "files:upload"],
    }
