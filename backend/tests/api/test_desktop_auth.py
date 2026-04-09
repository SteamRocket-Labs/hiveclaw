"""Tests for Desktop Auth Bridge endpoints (ARCHITECTURE.md §7.1).

Uses FastAPI TestClient with dependency overrides — no real DB or Feishu API required.
Tests cover CSRF nonce validation, error message sanitization, and auth guards.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.desktop_auth as desktop_auth_mod
from app.api.desktop_auth import router, _oauth_state_fallback
from app.core.security import get_current_user
from app.database import get_db


# ─── Fixtures & helpers ─────────────────────────────────

_USER_ID = uuid4()
_TENANT_ID = uuid4()

_FAKE_USER = SimpleNamespace(
    id=_USER_ID,
    username="zhangsan",
    email="zhangsan@test.com",
    display_name="张三",
    avatar_url=None,
    role="member",
    tenant_id=_TENANT_ID,
    is_active=True,
)


class _FakeDB:
    """Minimal async DB stand-in for TestClient dependency override."""

    def __init__(self):
        self.added = []
        self.flushed = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.added[0] if self.added else None)

    async def get(self, model, pk):
        if pk == _USER_ID:
            return _FAKE_USER
        return None


def _build_app() -> tuple[FastAPI, _FakeDB]:
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB()

    async def override_db():
        yield fake_db

    async def override_user():
        return _FAKE_USER

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    return app, fake_db


# ─── GET /auth/feishu/authorize ─────────────────────────


def test_authorize_redirects_to_feishu():
    """authorize must redirect to Feishu OAuth with CSRF nonce in state (not device_id)."""
    app, _ = _build_app()
    with patch.object(desktop_auth_mod, "settings", SimpleNamespace(
        FEISHU_APP_ID="cli_test_app_id",
        FEISHU_APP_SECRET="secret",
        FEISHU_REDIRECT_URI="",
        DESKTOP_DEEP_LINK_SCHEME="copaw",
    )):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/auth/feishu/authorize?device_id=dev123", follow_redirects=False)

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "open.feishu.cn" in location
    assert "cli_test_app_id" in location
    assert "callback-desktop" in location
    # state should be a CSRF nonce, NOT the raw device_id
    assert "state=" in location
    assert "state=dev123" not in location


def test_authorize_stores_nonce_in_fallback():
    """authorize must store a nonce→device_id mapping (falls back to memory when Redis unavailable)."""
    _oauth_state_fallback.clear()
    app, _ = _build_app()

    # Force fallback path by making Redis raise
    async def _no_redis():
        raise ConnectionError("no redis in test")

    with (
        patch.object(desktop_auth_mod, "settings", SimpleNamespace(
            FEISHU_APP_ID="cli_test_app_id",
            FEISHU_APP_SECRET="secret",
            DESKTOP_DEEP_LINK_SCHEME="copaw",
        )),
        patch("app.core.events.get_redis", new=_no_redis),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/auth/feishu/authorize?device_id=mydev", follow_redirects=False)

    assert len(_oauth_state_fallback) == 1
    stored_device_id = list(_oauth_state_fallback.values())[0]
    assert stored_device_id == "mydev"
    _oauth_state_fallback.clear()


def test_authorize_returns_503_when_feishu_not_configured():
    """authorize must fail gracefully if Feishu app is not configured."""
    app, _ = _build_app()
    with patch.object(desktop_auth_mod, "settings", SimpleNamespace(
        FEISHU_APP_ID="",
        FEISHU_APP_SECRET="",
        DESKTOP_DEEP_LINK_SCHEME="copaw",
    )):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/auth/feishu/authorize?device_id=dev1")

    assert resp.status_code == 503


def test_authorize_requires_device_id():
    """authorize must require device_id query parameter."""
    app, _ = _build_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/auth/feishu/authorize")
    assert resp.status_code == 422


# ─── GET /auth/feishu/callback-desktop ──────────────────


def test_callback_desktop_redirects_to_deep_link():
    """Successful callback with valid CSRF nonce must redirect to copaw:// deep link."""
    app, fake_db = _build_app()

    # Pre-populate CSRF nonce → device_id mapping
    test_nonce = "test_csrf_nonce_abc123"
    _oauth_state_fallback[test_nonce] = "dev1"

    fake_feishu_user = {
        "open_id": "ou_test123", "union_id": "on_test456", "user_id": "u_test789",
        "name": "张三", "email": "zhangsan@test.com", "avatar_url": "",
    }

    with (
        patch.object(desktop_auth_mod.feishu_service, "exchange_code_for_user", new_callable=AsyncMock, return_value=fake_feishu_user),
        patch.object(desktop_auth_mod.feishu_service, "login_or_register", new_callable=AsyncMock, return_value=(_FAKE_USER, "jwt_access_token_here")),
        patch.object(desktop_auth_mod, "create_refresh_token", new_callable=AsyncMock, return_value="raw_refresh_token_here"),
        patch.object(desktop_auth_mod, "settings", SimpleNamespace(DESKTOP_DEEP_LINK_SCHEME="copaw")),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/auth/feishu/callback-desktop?code=auth_code_123&state={test_nonce}", follow_redirects=False)

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("copaw://auth/callback?")
    assert "jwt_access_token_here" in location
    assert "raw_refresh_token_here" in location
    assert str(_USER_ID) in location
    # Nonce must be consumed (one-time use)
    assert test_nonce not in _oauth_state_fallback


def test_callback_desktop_rejects_invalid_state():
    """Callback with unknown/expired CSRF nonce must redirect to error deep link."""
    _oauth_state_fallback.clear()
    app, _ = _build_app()

    with patch.object(desktop_auth_mod, "settings", SimpleNamespace(DESKTOP_DEEP_LINK_SCHEME="copaw")):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/auth/feishu/callback-desktop?code=some_code&state=unknown_nonce", follow_redirects=False)

    assert resp.status_code == 302
    assert "copaw://auth/error?reason=invalid_state" in resp.headers["location"]


def test_callback_desktop_handles_feishu_error_without_leaking():
    """Feishu error must redirect to generic error, not leak exception details."""
    test_nonce = "nonce_for_error_test"
    _oauth_state_fallback[test_nonce] = "dev1"
    app, _ = _build_app()

    with (
        patch.object(desktop_auth_mod.feishu_service, "exchange_code_for_user", new_callable=AsyncMock, side_effect=RuntimeError("secret internal error: API key xxx")),
        patch.object(desktop_auth_mod, "settings", SimpleNamespace(DESKTOP_DEEP_LINK_SCHEME="copaw")),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/auth/feishu/callback-desktop?code=bad_code&state={test_nonce}", follow_redirects=False)

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "copaw://auth/error?reason=auth_failed" in location
    # Must NOT contain the internal error message
    assert "secret internal error" not in location
    assert "API key" not in location


# ─── POST /auth/desktop/exchange ────────────────────────


def test_exchange_returns_new_access_token():
    """Valid refresh token exchange must return a new access token."""
    app, _ = _build_app()
    token_row = SimpleNamespace(
        user_id=_USER_ID, revoked=False,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )

    with patch.object(desktop_auth_mod, "verify_refresh_token", new_callable=AsyncMock, return_value=token_row):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/auth/desktop/exchange", json={
            "refresh_token": "valid_raw_token", "device_id": "dev1",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_exchange_rejects_invalid_refresh_token():
    """Invalid refresh token must return 401."""
    from fastapi import HTTPException
    app, _ = _build_app()

    async def _reject(*a, **kw):
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    with patch.object(desktop_auth_mod, "verify_refresh_token", new=_reject):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/auth/desktop/exchange", json={
            "refresh_token": "garbage_token", "device_id": "dev1",
        })

    assert resp.status_code == 401


def test_exchange_rejects_wrong_device_id():
    """Exchange with a different device_id than the token was issued for must return 401."""
    from fastapi import HTTPException
    app, _ = _build_app()

    async def _reject_device(*a, **kw):
        raise HTTPException(status_code=401, detail="Device mismatch")

    with patch.object(desktop_auth_mod, "verify_refresh_token", new=_reject_device):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/auth/desktop/exchange", json={
            "refresh_token": "valid_token_wrong_device", "device_id": "attacker_device",
        })

    assert resp.status_code == 401
    assert "Device mismatch" in resp.json()["detail"]


def test_exchange_rejects_inactive_user():
    """Exchange must fail if the user has been deactivated."""
    app, _ = _build_app()
    token_row = SimpleNamespace(
        user_id=uuid4(), revoked=False,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )

    with patch.object(desktop_auth_mod, "verify_refresh_token", new_callable=AsyncMock, return_value=token_row):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/auth/desktop/exchange", json={
            "refresh_token": "token_for_gone_user", "device_id": "dev1",
        })

    assert resp.status_code == 401


# ─── POST /auth/desktop/logout ──────────────────────────


def test_logout_revokes_refresh_token():
    """Logout must call revoke and return 204 (requires valid JWT)."""
    app, _ = _build_app()

    with patch.object(desktop_auth_mod, "revoke_refresh_token", new_callable=AsyncMock) as mock_revoke:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/auth/desktop/logout", json={
            "refresh_token": "token_to_revoke",
            "device_id": "dev1",
        })

    assert resp.status_code == 204
    mock_revoke.assert_called_once()
