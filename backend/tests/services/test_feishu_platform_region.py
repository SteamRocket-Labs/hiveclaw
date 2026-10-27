from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


def test_http_service_import_does_not_load_the_native_feishu_sdk():
    script = """import sys
class RejectSDK:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'lark_oapi' or fullname.startswith('lark_oapi.'):
            raise AssertionError('HTTP service import loaded the native SDK')
sys.meta_path.insert(0, RejectSDK())
from app.config import Settings
Settings.model_config['env_file'] = None
from app.services.feishu_service import FeishuService
service = FeishuService()
assert 'lark_oapi' not in sys.modules
sys.meta_path.pop(0)
from app.services.feishu_service import lark_sdk_available
assert lark_sdk_available()
client = service._get_lark_client('synthetic-client', 'synthetic-secret')
assert service._get_lark_client('synthetic-client', 'synthetic-secret') is client
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
    from app.services.feishu_service import FeishuService, lark_sdk_available

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
    monkeypatch.setattr("app.services.feishu_service._load_lark_sdk", lambda: fake_lark)
    assert lark_sdk_available() is True

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


def test_missing_sdk_still_refuses_native_client_and_reports_unavailable(monkeypatch):
    from app.services.feishu_service import FeishuService, lark_sdk_available

    def unavailable():
        raise ImportError("Synthetic unavailable SDK")

    monkeypatch.setattr("app.services.feishu_service._load_lark_sdk", unavailable)
    assert lark_sdk_available() is False
    with pytest.raises(RuntimeError, match="lark-oapi package is not installed"):
        FeishuService()._get_lark_client("synthetic-app", "synthetic-secret")
