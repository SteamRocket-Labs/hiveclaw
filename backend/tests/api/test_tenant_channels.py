"""Tests for tenant-level channel config + enterprise webhook routing (Phase 6)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from app.api.tenant_channels import router
from app.core.security import get_current_admin
from app.database import get_db


# ─── Fixtures ───────────────────────────────────────────

_TENANT_ID = uuid4()
_USER_ID = uuid4()
_MAIN_AGENT_ID = uuid4()

_ADMIN = SimpleNamespace(
    id=uuid4(),
    username="admin",
    role="org_admin",
    tenant_id=_TENANT_ID,
    is_active=True,
)

_EXISTING_CONFIG = SimpleNamespace(
    id=uuid4(),
    tenant_id=_TENANT_ID,
    channel_type="feishu",
    app_id="cli_company",
    app_secret="secret",
    encrypt_key="enc123",
    verification_token="vt123",
    extra_config={},
    is_active=True,
)


class _SingleScalar:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v


class _ListScalars:
    def __init__(self, values):
        self._v = values

    def scalars(self):
        return self

    def all(self):
        return self._v


class _FakeDB:
    def __init__(self, *, configs=None, config=None, user=None, agent_id=None):
        self._configs = configs or []
        self._config = config
        self._user = user
        self._agent_id = agent_id
        self.sync_session = SimpleNamespace(info={})
        self.statements: list[str] = []
        self.added = []
        self.deleted = []
        self._call_idx = 0

    async def execute(self, stmt):
        self._call_idx += 1
        stmt_str = str(stmt)
        self.statements.append(stmt_str)
        if "SET LOCAL app.current_tenant_id" in stmt_str:
            return _SingleScalar(None)
        # List query (scalars().all())
        if "tenant_channel_configs" in stmt_str and self._call_idx == 1 and self._configs:
            return _ListScalars(self._configs)
        # Single config lookup
        if "tenant_channel_configs" in stmt_str:
            return _SingleScalar(self._config)
        # User lookup (sender routing)
        if "users" in stmt_str:
            return _SingleScalar(self._user)
        # Agent lookup (sender routing)
        if "agents" in stmt_str:
            return _SingleScalar(self._agent_id)
        return _SingleScalar(None)

    def add(self, obj):
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid4()
        self.added.append(obj)

    async def flush(self):
        pass

    async def delete(self, obj):
        self.deleted.append(obj)


def _build_client(*, configs=None, config=None, user=None, agent_id=None):
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB(configs=configs, config=config, user=user, agent_id=agent_id)

    async def override_admin():
        return _ADMIN

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_admin] = override_admin
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db


# ─── GET /tenant-channels ───────────────────────────────


def test_list_tenant_channels():
    client, _ = _build_client(configs=[_EXISTING_CONFIG])
    resp = client.get("/tenant-channels")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["channel_type"] == "feishu"
    assert data[0]["app_id"] == "cli_company"
    # app_secret must NOT be in the output schema
    assert "app_secret" not in data[0]


def test_list_tenant_channels_never_returns_extra_config_secrets():
    config = SimpleNamespace(
        **{
            **vars(_EXISTING_CONFIG),
            "extra_config": {
                "region": "cn",
                "client_secret": "tenant-client-secret",
                "nested": {"bot_token": "tenant-bot-token"},
            },
        }
    )
    client, _ = _build_client(configs=[config])

    response = client.get("/tenant-channels")

    assert response.status_code == 200
    extra = response.json()[0]["extra_config"]
    assert extra == {"region": "cn", "client_secret": "****", "nested": {"bot_token": "****"}}


# ─── PUT /tenant-channels/{type} ────────────────────────


def test_upsert_creates_new_config():
    client, fake_db = _build_client(config=None)
    resp = client.put(
        "/tenant-channels/feishu",
        json={
            "app_id": "cli_new",
            "app_secret": "secret_new",
            "encrypt_key": "enc",
            "verification_token": "vt",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["app_id"] == "cli_new"
    assert len(fake_db.added) == 1


def test_upsert_updates_existing_config():
    client, _ = _build_client(config=_EXISTING_CONFIG)
    resp = client.put(
        "/tenant-channels/feishu",
        json={
            "app_id": "cli_updated",
            "app_secret": "new_secret",
        },
    )
    assert resp.status_code == 200
    assert _EXISTING_CONFIG.app_id == "cli_updated"


# ─── DELETE /tenant-channels/{type} ─────────────────────


def test_delete_channel():
    client, fake_db = _build_client(config=_EXISTING_CONFIG)
    resp = client.delete("/tenant-channels/feishu")
    assert resp.status_code == 204
    assert len(fake_db.deleted) == 1


def test_delete_nonexistent():
    client, _ = _build_client(config=None)
    resp = client.delete("/tenant-channels/feishu")
    assert resp.status_code == 404


# ─── GET /tenant-channels/{type}/webhook-url ────────────


def test_webhook_url():
    client, _ = _build_client()
    resp = client.get("/tenant-channels/feishu/webhook-url")
    assert resp.status_code == 200
    url = resp.json()["webhook_url"]
    assert f"/channel/feishu/tenant/{_TENANT_ID}/webhook" in url


# ─── POST /channel/feishu/tenant/{id}/webhook ───────────


def test_webhook_challenge_response():
    """Feishu URL verification must echo challenge back."""
    from app import database

    app = FastAPI()
    app.include_router(router)
    config = SimpleNamespace(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        channel_type="feishu",
        app_id="cli_company",
        app_secret="secret",
        encrypt_key=None,
        verification_token=None,
        extra_config={},
        is_active=True,
    )
    fake_db = _FakeDB(config=config)

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        f"/channel/feishu/tenant/{_TENANT_ID}/webhook",
        json={
            "challenge": "test_challenge_token",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["challenge"] == "test_challenge_token"
    assert fake_db.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(_TENANT_ID)
    assert any(f"SET LOCAL app.current_tenant_id = '{_TENANT_ID}'" in stmt for stmt in fake_db.statements)


def test_webhook_routes_to_main_agent():
    """Valid sender with Main Agent must be routed correctly."""
    fake_user = SimpleNamespace(id=_USER_ID, feishu_user_id="fu_123", tenant_id=_TENANT_ID)

    app = FastAPI()
    app.include_router(router)
    unsigned_config = SimpleNamespace(
        **{
            **vars(_EXISTING_CONFIG),
            "encrypt_key": None,
            "verification_token": None,
        }
    )
    fake_db = _FakeDB(config=unsigned_config, user=fake_user, agent_id=_MAIN_AGENT_ID)

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_db

    feishu_event = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "ev_test"},
        "event": {
            "sender": {"sender_id": {"user_id": "fu_123", "open_id": "ou_123"}},
            "message": {"message_type": "text", "content": '{"text":"hello"}'},
        },
    }

    with patch(
        "app.services.channel_ingress_inbox.accept_authenticated_channel_event",
        new_callable=AsyncMock,
    ) as mock_accept:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/channel/feishu/tenant/{_TENANT_ID}/webhook", json=feishu_event)

    assert resp.status_code == 200
    assert resp.json()["routed"] is True
    mock_accept.assert_awaited_once()
    call_kwargs = mock_accept.await_args.kwargs
    assert call_kwargs["tenant_id"] == _TENANT_ID
    assert call_kwargs["agent_id"] == _MAIN_AGENT_ID
    assert call_kwargs["provider_event_id"] == "ev_test"
    assert call_kwargs["handler_key"] == "feishu.event"


def test_webhook_unknown_sender_not_routed():
    """Sender with no User/Agent returns routed=False."""
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB(config=_EXISTING_CONFIG, user=None, agent_id=None)

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_db

    feishu_event = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "ev_unknown"},
        "event": {
            "sender": {"sender_id": {"user_id": "unknown", "open_id": "ou_unknown"}},
            "message": {"message_type": "text", "content": '{"text":"who am i"}'},
        },
    }

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(f"/channel/feishu/tenant/{_TENANT_ID}/webhook", json=feishu_event)
    assert resp.status_code == 200
    assert resp.json()["routed"] is False


def test_webhook_no_tenant_config():
    """Webhook to tenant with no config returns 404."""
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB(config=None)

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_db

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/channel/feishu/tenant/{_TENANT_ID}/webhook",
        json={
            "header": {"event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_id": {"user_id": "x"}}},
        },
    )
    assert resp.status_code == 404
