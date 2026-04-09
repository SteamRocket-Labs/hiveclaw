from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock, patch

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
    def __init__(self, session=None):
        self.session = session
        self.committed = False
        self.flushed = False

    async def get(self, model, pk):
        return self.session

    async def commit(self):
        self.committed = True

    async def flush(self):
        self.flushed = True


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
