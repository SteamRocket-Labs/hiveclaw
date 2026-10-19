"""Office helpers send credentials only to their connection-selected platform."""

import asyncio

import httpx
import pytest

from app.services.agent_tool_domains.feishu_helpers import (
    FeishuAccessToken, _request_feishu_tenant_access_token, _get_agent_calendar_id, _feishu_resolve_open_id,
)
from app.services.feishu_platform import resolve_feishu_platform


@pytest.mark.parametrize("config,origin", [
    ({}, "https://open.feishu.cn"),
    ({"platform_region": "lark_global"}, "https://open.larksuite.com"),
    ({"platform_region": "custom", "open_api_domain": "https://office.fixture.test"}, "https://office.fixture.test"),
])
async def test_token_and_office_helpers_keep_the_selected_origin(monkeypatch, config, origin):
    from app.services.agent_tool_domains import feishu_base, feishu_tasks, feishu_wiki, feishu_sheets, feishu_drive

    def provider(request):
        assert str(request.url).startswith(origin + "/open-apis/"), request.url
        if "/auth/v3/" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-token"})
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        path = request.url.path
        if path.endswith("/download"):
            return httpx.Response(200, content=b"OFFICE_RESULT", headers={"content-type": "application/octet-stream"})
        payload = {"calendars": [{"calendar": {"calendar_id": "calendar"}}], "user_list": [{"user_id": "ou_user"}],
            "node": {"obj_token": "document"}, "sheets": [{"sheet_id": "sheet"}], "value": "OFFICE_RESULT"}
        return httpx.Response(200, json={"code": 0, "data": payload})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))
    auth = await _request_feishu_tenant_access_token("synthetic-app", "synthetic-secret", platform=resolve_feishu_platform(config))
    token = auth["token"]
    assert token == "synthetic-token" and isinstance(token, FeishuAccessToken)
    assert await _get_agent_calendar_id(token) == ("calendar", None)
    assert await _feishu_resolve_open_id(token, "reader@fixture.test") == "ou_user"
    assert (await feishu_wiki._feishu_wiki_get_node("node", token))["obj_token"] == "document"
    assert await feishu_sheets._get_first_sheet_id_via_openapi("spreadsheet", token) == "sheet"
    content, _, _ = await feishu_drive._download_drive_file("file", token)
    assert content == b"OFFICE_RESULT"
    for method, body in [("GET", None), ("POST", {}), ("PUT", {}), ("DELETE", None)]:
        args = [token, "/bitable/v1/apps/base/tables"] + ([body] if body is not None else [])
        assert (await getattr(feishu_base, "_base_api_" + method.lower())(*args))["value"] == "OFFICE_RESULT"
    assert (await feishu_tasks._task_api_request("GET", token, "/task/v2/tasks"))["value"] == "OFFICE_RESULT"


async def test_interleaved_tokens_with_identical_bytes_keep_independent_platforms(monkeypatch):
    from app.services.agent_tool_domains.feishu_base import _base_api_get

    origins = {"global": "https://open.larksuite.com", "private": "https://office.fixture.test"}
    def provider(request):
        key = request.url.path.rsplit("/", 1)[-1]
        assert str(request.url) == origins[key] + "/open-apis/bitable/v1/apps/" + key
        assert request.headers["Authorization"] == "Bearer synthetic-shared-token"
        return httpx.Response(200, json={"code": 0, "data": {"origin": origins[key]}})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))
    tokens = {
        "global": FeishuAccessToken("synthetic-shared-token", resolve_feishu_platform({"platform_region": "lark_global"})),
        "private": FeishuAccessToken("synthetic-shared-token", resolve_feishu_platform({"platform_region": "custom",
            "open_api_domain": origins["private"]})),
    }
    results = await asyncio.gather(*(_base_api_get(token, "/bitable/v1/apps/" + key) for key, token in tokens.items()))
    assert [r["origin"] for r in results] == list(origins.values())
