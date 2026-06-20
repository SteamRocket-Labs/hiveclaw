from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_feishu_platform_resolver_maps_lark_global_domains():
    from app.services.feishu_platform import resolve_feishu_platform

    platform = resolve_feishu_platform({"platform_region": "lark_global"})

    assert platform.region == "lark_global"
    assert platform.open_api_domain == "https://open.larksuite.com"
    assert platform.open_api_base_url == "https://open.larksuite.com/open-apis"


def test_feishu_platform_resolver_keeps_existing_configs_on_feishu_cn():
    from app.services.feishu_platform import resolve_feishu_platform

    platform = resolve_feishu_platform({})

    assert platform.region == "feishu_cn"
    assert platform.open_api_domain == "https://open.feishu.cn"
    assert platform.open_api_base_url == "https://open.feishu.cn/open-apis"


@pytest.mark.asyncio
async def test_feishu_service_send_message_uses_lark_global_domain(monkeypatch):
    from app.services.feishu_service import FeishuService

    requests: list[tuple[str, str]] = []

    class _FakeResponse:
        status_code = 200

        def __init__(self, payload: dict):
            self._payload = payload

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            requests.append(("POST", url))
            if url.endswith("/auth/v3/app_access_token/internal"):
                return _FakeResponse({"code": 0, "tenant_access_token": "tenant-token"})
            return _FakeResponse({"code": 0, "data": {"message_id": "om_1"}})

    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", _FakeClient)

    await FeishuService().send_message(
        "cli_lark",
        "secret",
        "ou_user",
        "text",
        '{"text":"hi"}',
        extra_config={"platform_region": "lark_global"},
    )

    assert requests == [
        ("POST", "https://open.larksuite.com/open-apis/auth/v3/app_access_token/internal"),
        ("POST", "https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=open_id"),
    ]


def test_feishu_lark_client_builder_receives_resolved_domain(monkeypatch):
    from app.services.feishu_service import FeishuService

    captured: dict[str, object] = {}

    class _FakeBuilder:
        def app_id(self, value):
            captured["app_id"] = value
            return self

        def app_secret(self, value):
            captured["app_secret"] = value
            return self

        def domain(self, value):
            captured["domain"] = value
            return self

        def build(self):
            return object()

    fake_lark = SimpleNamespace(Client=SimpleNamespace(builder=lambda: _FakeBuilder()))
    monkeypatch.setattr("app.services.feishu_service._HAS_LARK", True)
    monkeypatch.setattr("app.services.feishu_service.lark", fake_lark)

    FeishuService()._get_lark_client(
        "cli_lark",
        "secret",
        extra_config={"platform_region": "lark_global"},
    )

    assert captured == {
        "app_id": "cli_lark",
        "app_secret": "secret",
        "domain": "https://open.larksuite.com",
    }
