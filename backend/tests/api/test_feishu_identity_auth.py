from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.feishu import router
from app.core.security import get_current_user
from app.database import get_db


def _fake_user(tenant_id=None):
    return SimpleNamespace(
        id=uuid4(),
        username="feishu_user",
        email="feishu@test.com",
        display_name="飞书用户",
        avatar_url=None,
        role="member",
        tenant_id=tenant_id,
        department_id=None,
        title=None,
        feishu_open_id="ou_test_open_id",
        oidc_sub=None,
        is_active=True,
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
        tokens_used_today=0,
        tokens_used_month=0,
        tokens_used_total=0,
        created_at=datetime.now(timezone.utc),
    )


class _FakeDB:
    def __init__(self, session=None, execute_results=None):
        self.session = session
        self.execute_results = list(execute_results or [])
        self.committed = False
        self.flushed = False
        self.added = []

    async def get(self, model, pk):
        return self.session

    async def execute(self, _stmt):
        if not self.execute_results:
            raise AssertionError("Unexpected execute() call")

        value = self.execute_results.pop(0)

        class _Result:
            def __init__(self, item):
                self.item = item

            def scalar_one_or_none(self):
                return self.item

        return _Result(value)

    async def commit(self):
        self.committed = True

    async def flush(self):
        self.flushed = True

    def add(self, value):
        self.added.append(value)


def _build_app(db: _FakeDB, current_user=None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield db

    async def override_user():
        return current_user or _fake_user()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    return app


def test_feishu_callback_post_uses_provider_driven_auth():
    tenant_id = uuid4()
    db = _FakeDB()
    app = _build_app(db)
    user = _fake_user(tenant_id)

    with patch(
        "app.api.feishu.feishu_auth_provider.authenticate_with_code",
        new_callable=AsyncMock,
        return_value=(user, "jwt-token"),
    ) as auth_mock:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(f"/auth/feishu/callback?code=oauth-code&tenant_id={tenant_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "jwt-token"
    assert body["user"]["id"] == str(user.id)
    auth_mock.assert_awaited_once()


def test_feishu_sso_init_uses_current_oauth_authorize_contract(monkeypatch: pytest.MonkeyPatch):
    db = _FakeDB()
    app = _build_app(db)

    settings = SimpleNamespace(
        FEISHU_APP_ID="cli_test_app_id",
        FEISHU_APP_SECRET="secret",
        FEISHU_REDIRECT_URI="https://example.com/api/auth/feishu/callback",
    )
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/auth/feishu/sso/init")

    assert response.status_code == 200
    payload = response.json()
    assert payload["authorize_url"].startswith("https://accounts.feishu.cn/open-apis/authen/v1/authorize?")
    assert "client_id=cli_test_app_id" in payload["authorize_url"]
    assert "response_type=code" in payload["authorize_url"]
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fapi%2Fauth%2Ffeishu%2Fcallback" in payload["authorize_url"]
    assert db.added, "Expected an SSO session row to be created"


def test_feishu_sso_init_returns_503_when_platform_env_missing(monkeypatch: pytest.MonkeyPatch):
    """Deployment hasn't registered a Feishu ISV app → refuse, don't build empty client_id= URL."""
    db = _FakeDB()
    app = _build_app(db)

    settings = SimpleNamespace(FEISHU_APP_ID=None, FEISHU_REDIRECT_URI=None)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/auth/feishu/sso/init")

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"].lower()
    assert not db.added, "No SSO session should be created when platform is unconfigured"


def test_feishu_bind_init_returns_503_when_platform_env_missing(monkeypatch: pytest.MonkeyPatch):
    """Same preflight on the bind flow."""
    db = _FakeDB()
    app = _build_app(db)

    settings = SimpleNamespace(FEISHU_APP_ID="", FEISHU_REDIRECT_URI="")
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/auth/feishu/bind/init")

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"].lower()


def test_feishu_bind_init_uses_tenant_oauth_config_when_platform_env_missing(monkeypatch: pytest.MonkeyPatch):
    """Tenant Feishu settings are the source of truth in multi-tenant production."""
    tenant_id = uuid4()
    current_user = _fake_user(tenant_id)
    tenant_setting = SimpleNamespace(value={"app_id": "tenant_app_id", "app_secret": "tenant_secret"})
    db = _FakeDB(execute_results=[tenant_setting])
    app = _build_app(db, current_user=current_user)

    settings = SimpleNamespace(
        FEISHU_APP_ID="",
        FEISHU_APP_SECRET="",
        FEISHU_REDIRECT_URI="",
        PUBLIC_BASE_URL="https://backend.example.com",
        BASE_URL="",
    )
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/auth/feishu/bind/init")

    assert response.status_code == 200
    payload = response.json()
    assert "client_id=tenant_app_id" in payload["authorize_url"]
    assert "redirect_uri=https%3A%2F%2Fbackend.example.com%2Fapi%2Fauth%2Ffeishu%2Fcallback" in payload["authorize_url"]
    assert db.added[0].tenant_id == tenant_id
    assert db.added[0].user_id == current_user.id
    assert db.added[0].status == "pending_bind"


def test_feishu_sso_init_returns_503_when_only_redirect_uri_missing(monkeypatch: pytest.MonkeyPatch):
    """Pin OR short-circuit: partial config is still broken config."""
    db = _FakeDB()
    app = _build_app(db)

    settings = SimpleNamespace(FEISHU_APP_ID="cli_test_app_id", FEISHU_REDIRECT_URI="")
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/auth/feishu/sso/init")

    assert response.status_code == 503


def test_feishu_bind_init_returns_503_when_only_app_id_missing(monkeypatch: pytest.MonkeyPatch):
    """Pin OR short-circuit for the bind path too."""
    db = _FakeDB()
    app = _build_app(db)

    settings = SimpleNamespace(FEISHU_APP_ID=None, FEISHU_REDIRECT_URI="https://example.com/cb")
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/auth/feishu/bind/init")

    assert response.status_code == 503


def test_feishu_callback_get_completes_scan_session_and_returns_html_redirect():
    tenant_id = uuid4()
    session_id = uuid4()
    user = _fake_user(tenant_id)
    session = SimpleNamespace(
        id=session_id,
        tenant_id=tenant_id,
        status="pending",
        provider_type=None,
        user_id=None,
        access_token=None,
        error_msg=None,
    )
    db = _FakeDB(session=session)
    app = _build_app(db)

    with patch(
        "app.api.feishu.feishu_auth_provider.authenticate_with_code",
        new_callable=AsyncMock,
        return_value=(user, "jwt-token"),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/auth/feishu/callback?code=oauth-code&state={session_id}")

    assert response.status_code == 200
    assert f"/sso/entry?sid={session_id}&complete=1" in response.text
    assert session.status == "completed"
    assert session.provider_type == "feishu"
    assert session.user_id == user.id
    assert session.access_token == "jwt-token"
    assert db.committed is True


def test_bind_feishu_account_uses_provider_binding():
    tenant_id = uuid4()
    current_user = _fake_user(tenant_id)
    db = _FakeDB()
    app = _build_app(db, current_user=current_user)

    with patch(
        "app.api.feishu.feishu_auth_provider.bind_with_code",
        new_callable=AsyncMock,
        return_value=current_user,
    ) as bind_mock:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/auth/feishu/bind?code=bind-code")

    assert response.status_code == 200
    assert response.json()["id"] == str(current_user.id)
    bind_mock.assert_awaited_once()


def test_feishu_card_callback_resolves_user_via_channel_user_service():
    tenant_id = uuid4()
    approval_id = uuid4()
    agent_id = uuid4()
    current_user = _fake_user(tenant_id)
    approval = SimpleNamespace(id=approval_id, agent_id=agent_id, action_type="publish")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    db = _FakeDB(execute_results=[approval, agent])
    app = _build_app(db)

    callback_body = {
        "open_id": "ou_callback_open",
        "operator": {"open_id": "ou_callback_open"},
        "action": {
            "value": '{"approval_id": "%s", "action": "approve"}' % approval_id
        },
    }

    with patch(
        "app.api.feishu.channel_user_service.resolve_feishu_user",
        new_callable=AsyncMock,
        return_value=current_user,
    ) as resolve_user_mock, patch(
        "app.services.approval_service.approval_service.resolve_approval",
        new_callable=AsyncMock,
        return_value=approval,
    ) as resolve_approval_mock, patch(
        "app.core.policy.write_audit_event",
        new_callable=AsyncMock,
    ):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/channel/feishu/card-callback", json=callback_body)

    assert response.status_code == 200
    resolve_user_mock.assert_awaited_once()
    resolve_approval_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_feishu_event_dispatches_card_action_trigger_to_callback(monkeypatch: pytest.MonkeyPatch):
    import app.api.feishu as feishu_api

    captured: dict[str, object] = {}

    async def fake_card_callback(request, db):
        captured["payload"] = await request.json()
        return {"ok": True}

    monkeypatch.setattr(feishu_api, "feishu_card_callback", fake_card_callback)

    body = {
        "header": {"event_type": "card.action.trigger", "event_id": "evt_card_1"},
        "event": {
            "operator": {"open_id": "ou_card_1"},
            "action": {"value": "{\"approval_id\": \"123\", \"action\": \"approve\"}"},
        },
    }

    result = await feishu_api.process_feishu_event(
        uuid4(),
        body,
        _FakeDB(),
        tenant_channel_config=SimpleNamespace(),
    )

    assert result == {"ok": True}
    assert captured["payload"] == body["event"]
