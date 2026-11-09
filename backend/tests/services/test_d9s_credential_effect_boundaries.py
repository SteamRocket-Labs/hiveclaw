"""Provider-controlled output and targets cannot release stored credentials."""

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "exception"])
async def test_admin_test_redacts_provider_echo(monkeypatch, outcome):
    from app.api import custom_api_connectors as api
    from app.core.security import get_current_admin
    from app.database import get_db

    secret = "D9sSyntheticAdminEchoValue0123456789AbCdEf"
    user = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    tool = SimpleNamespace(id=uuid4(), name="custom_api__fixture__read", config={
        "base_url": "https://api.example.test",
        "auth": {"scheme": "bearer"}, "action": {"path": "/read"},
    })

    async def connector(*args):
        return tool

    async def config(*args, **kwargs):
        return {"bearer_token": secret}

    async def provider(method, url, **kwargs):
        assert kwargs["headers"]["Authorization"] == f"Bearer {secret}"
        if outcome == "exception":
            raise RuntimeError(f"Provider rejected {secret}")
        return httpx.Response(200 if outcome == "success" else 422,
            json={"echo": secret, "business_value": "preserve this"},
            headers={"X-Provider-Echo": secret})

    monkeypatch.setattr(api, "_get_connector_or_404", connector)
    monkeypatch.setattr(api, "resolve_tool_config", config)
    monkeypatch.setattr("app.services.governed_egress.request_governed_http", provider)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_admin] = lambda: user
    app.dependency_overrides[get_db] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hive.test") as client:
        response = await client.post(f"/enterprise/custom-api-connectors/{tool.id}/test", json={"arguments": {}})
    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert "[REDACTED_SECRET]" in response.json()["result"]
    assert response.json()["ok"] is (outcome == "success")
    if outcome != "exception":
        assert "preserve this" in response.json()["result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [
    "https://other.example/messages", "//other.example/messages",
    "http://mcp.example/messages", "https://mcp.example:444/messages",
])
@pytest.mark.parametrize("entry", ["request", "connect"])
async def test_mcp_rejects_cross_origin_sse_endpoint_before_post(monkeypatch, endpoint, entry):
    from app.services.mcp_client import MCPClient

    requests = []

    async def provider(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, text=f"event: endpoint\ndata: {endpoint}\n\n",
                headers={"content-type": "text/event-stream"})
        if request.url.path == "/sse":
            return httpx.Response(405)
        return httpx.Response(200, json={"result": {"tools": []}})

    client_type = httpx.AsyncClient
    monkeypatch.setattr("app.services.mcp_client.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(provider), **kwargs))
    client = MCPClient("https://mcp.example/sse", api_key="D9sSyntheticMcpBearer")
    with pytest.raises(Exception, match="连接地址与配置不一致") as failure:
        await (client.list_tools() if entry == "request" else client._sse_connect())
    assert str(failure.value) == "连接地址与配置不一致，请管理员检查应用连接设置。"
    assert not [r for r in requests if r.method == "POST" and r.url.path != "/sse"]
    assert all(r.url.host == "mcp.example" for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/messages", "https://MCP.EXAMPLE:443/messages"])
async def test_mcp_same_origin_endpoint_still_executes(monkeypatch, endpoint):
    from app.services.mcp_client import MCPClient

    posted = []

    async def provider(request):
        if request.method == "GET":
            return httpx.Response(200, text=f"event: endpoint\ndata: {endpoint}\n\n",
                headers={"content-type": "text/event-stream"})
        posted.append(request)
        assert request.headers["Authorization"] == "Bearer D9sSyntheticMcpBearer"
        return httpx.Response(200, json={"result": {"tools": [{"name": "read"}]}})

    client_type = httpx.AsyncClient
    monkeypatch.setattr("app.services.mcp_client.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(provider), **kwargs))
    client = MCPClient("https://mcp.example/sse", api_key="D9sSyntheticMcpBearer")
    client._transport = "sse"
    assert (await client.list_tools())[0]["name"] == "read"
    assert len(posted) == 3


@pytest.mark.parametrize("path", [
    "https://other.example/x", "//evil.example/x",
    "http://api.example/x", "https://api.example:444/x",
])
def test_custom_api_rejects_cross_origin_action(path):
    from app.services.custom_api_connectors import CustomApiConnectorError, prepare_custom_api_request

    with pytest.raises(CustomApiConnectorError, match="连接地址与配置不一致"):
        prepare_custom_api_request(tool_name="custom_api__fixture__read", arguments={},
            tool_config={"base_url": "https://api.example/v1", "action": {"path": path},
                "auth": {"scheme": "bearer"}}, secret_config={"bearer_token": "D9sSyntheticToken"})


@pytest.mark.parametrize("path", ["/read", "https://API.EXAMPLE:443/v1/read"])
def test_custom_api_same_origin_action_still_injects(path):
    from app.services.custom_api_connectors import prepare_custom_api_request

    prepared = prepare_custom_api_request(tool_name="custom_api__fixture__read", arguments={},
        tool_config={"base_url": "https://api.example/v1", "action": {"path": path},
            "auth": {"scheme": "bearer"}}, secret_config={"bearer_token": "D9sSyntheticToken"})
    assert prepared.headers["Authorization"] == "Bearer D9sSyntheticToken"
    assert prepared.url.lower().removeprefix("https://").replace(":443", "") == "api.example/v1/read"
