from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeResponse:
    def __init__(self, *, status_code: int, payload: dict | None = None, json_error: Exception | None = None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._responses.pop(0)

    async def patch(self, url: str, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_get_tenant_access_token_prefers_tenant_token(monkeypatch):
    from app.services.feishu_service import FeishuService

    fake_client = _FakeAsyncClient([
        _FakeResponse(status_code=200, payload={"tenant_access_token": "tenant-token", "app_access_token": "app-token"}),
    ])
    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    service = FeishuService()
    token = await service.get_tenant_access_token("tenant-app", "tenant-secret")

    assert token == "tenant-token"
    assert fake_client.calls[0][0] == "POST"
    assert fake_client.calls[0][2]["json"] == {"app_id": "tenant-app", "app_secret": "tenant-secret"}


@pytest.mark.asyncio
async def test_send_message_raises_with_stage_on_invalid_json(monkeypatch):
    from app.services.feishu_service import FeishuService

    fake_client = _FakeAsyncClient([
        _FakeResponse(status_code=200, payload={"tenant_access_token": "tenant-token"}),
        _FakeResponse(status_code=200, json_error=ValueError("not json")),
    ])
    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    service = FeishuService()

    with pytest.raises(RuntimeError, match="send_text"):
        await service.send_message(
            "tenant-app",
            "tenant-secret",
            "ou_xxx",
            "text",
            "{\"text\":\"hi\"}",
            receive_id_type="open_id",
            stage="send_text",
        )


@pytest.mark.asyncio
async def test_patch_message_raises_with_stage_on_business_error(monkeypatch):
    from app.services.feishu_service import FeishuService

    fake_client = _FakeAsyncClient([
        _FakeResponse(status_code=200, payload={"tenant_access_token": "tenant-token"}),
        _FakeResponse(status_code=200, payload={"code": 999001, "msg": "rate limited"}),
    ])
    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    service = FeishuService()

    with pytest.raises(RuntimeError, match="stream_patch"):
        await service.patch_message(
            "tenant-app",
            "tenant-secret",
            "om_dc132",
            "{\"content\":\"...\"}",
            stage="stream_patch",
        )

