from __future__ import annotations

import pytest

from app.services.auth_provider import FeishuAuthProvider


@pytest.mark.asyncio
async def test_exchange_code_for_user_uses_config_redirect_uri(monkeypatch: pytest.MonkeyPatch):
    captured_posts: list[dict] = []

    class _FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json):
            captured_posts.append({"url": url, "json": json})
            return _FakeResponse({"access_token": "user-access-token"})

        async def get(self, url, headers):
            return _FakeResponse(
                {
                    "data": {
                        "user_id": "ou_user",
                        "open_id": "ou_open",
                        "union_id": "on_union",
                        "name": "Tenant User",
                        "email": "tenant@example.com",
                    }
                }
            )

    monkeypatch.setattr("app.services.auth_provider.httpx.AsyncClient", _FakeClient)

    provider = FeishuAuthProvider()
    profile = await provider._exchange_code_for_user(
        {
            "app_id": "tenant_app_id",
            "app_secret": "tenant_secret",
            "redirect_uri": "https://backend.example.com/api/auth/feishu/callback",
        },
        "oauth-code",
    )

    assert profile["user_id"] == "ou_user"
    assert captured_posts[0]["json"]["client_id"] == "tenant_app_id"
    assert captured_posts[0]["json"]["redirect_uri"] == "https://backend.example.com/api/auth/feishu/callback"
