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


def test_pairing_init_defaults_to_hive_connect_client_kind(monkeypatch) -> None:
    captured = {}

    async def fake_create_pairing_session(db, request, *, base_url):
        captured["request"] = request
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
            "device_fingerprint": "fp-1",
        },
    )

    assert resp.status_code == 201
    assert captured["request"].client_kind == "hive-connect"


def test_install_guide_exposes_hive_connect_only() -> None:
    client, _ = _client()

    resp = client.get("/local-bridge/install-guide")

    assert resp.status_code == 200
    body = resp.json()
    assert body["product_name"] == "Hive Connect"
    assert body["skill_name"] == "hive-connect"
    assert body["skill_repo_url"] == "https://github.com/rocky2431/hive-connect-skill"
    assert body["npm_package"] == "@hiveclaw243/hive-connect"
    assert body["binary_name"] == "hive-connect"
    assert body["login_command"] == "hive-connect login"
    serialized = str(body)
    assert "--hive-url" not in serialized
    assert "hive-bridge" not in serialized.lower()
    assert "cc-connect" not in serialized.lower()


def test_approve_pairing_binds_current_user_tenant_and_default_local_agent(monkeypatch) -> None:
    tenant_id = uuid4()
    local_agent_id = uuid4()
    current_user = _user()
    current_user.tenant_id = tenant_id
    captured = {}

    async def fake_ensure_default_local_agent_for_pairing(db, *, user_code, user_id, tenant_id):
        captured["ensure_agent"] = {
            "user_code": user_code,
            "user_id": user_id,
            "tenant_id": tenant_id,
        }
        return SimpleNamespace(id=local_agent_id)

    async def fake_approve_pairing(db, *, user_code, user_id, tenant_id, agent_id=None, metadata):
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
            "agent_id": str(agent_id) if agent_id else None,
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
        }

    monkeypatch.setattr(
        local_bridge_api.bridge_service,
        "ensure_default_local_agent_for_pairing",
        fake_ensure_default_local_agent_for_pairing,
        raising=False,
    )
    monkeypatch.setattr(local_bridge_api.bridge_service, "approve_pairing_session", fake_approve_pairing)
    client, _ = _client(user=current_user)

    resp = client.post("/local-bridge/pairings/HIVE-ABCD/approve")

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert captured["user_code"] == "HIVE-ABCD"
    assert captured["user_id"] == current_user.id
    assert captured["tenant_id"] == tenant_id
    assert captured["ensure_agent"] == {
        "user_code": "HIVE-ABCD",
        "user_id": current_user.id,
        "tenant_id": tenant_id,
    }
    assert captured["agent_id"] == local_agent_id
    assert captured["metadata"]["approval_surface"] == "local_agents_page"


def test_approve_pairing_requires_current_user_tenant(monkeypatch) -> None:
    async def should_not_approve(*_args, **_kwargs):
        raise AssertionError("approve must not run without a current tenant")

    monkeypatch.setattr(local_bridge_api.bridge_service, "approve_pairing_session", should_not_approve)
    user = _user()
    user.tenant_id = None
    client, _ = _client(user=user)

    resp = client.post("/local-bridge/pairings/HIVE-ABCD/approve")

    assert resp.status_code == 400


def test_pairing_exchange_returns_token_only_after_approval(monkeypatch) -> None:
    device_code = "dev_secret_value"

    async def fake_exchange_pairing_session(db, *, device_code):
        assert device_code == "dev_secret_value"
        return {
            "status": "active",
            "access_token": "hb_secret",
            "token_type": "Bearer",
            "agent_id": None,
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
    tenant_id = uuid4()
    user_id = uuid4()
    context = BridgeAuthContext(
        connection_id=connection_id,
        tenant_id=tenant_id,
        agent_id=None,
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
        "agent_id": None,
        "user_id": str(user_id),
        "client_kind": "generic_mcp_stdio",
        "device_name": "Workstation",
        "scopes": ["gateway:poll", "files:upload"],
    }
