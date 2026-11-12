"""MCP imports and stored endpoints may only reach public HTTP addresses."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.services import governed_egress
from app.services.mcp_authz import MCPAuthzError


PRIVATE_URLS = [
    "http://127.0.0.1/mcp", "http://[::1]/mcp", "http://10.0.0.1/mcp",
    "http://172.16.0.1/mcp", "http://192.168.1.1/mcp", "http://169.254.169.254/mcp",
    "http://100.64.0.1/mcp", "http://[fe80::1]/mcp", "http://metadata.google.internal/mcp",
]


@pytest.mark.parametrize("entry", ["direct", "bot", "command", "admin"])
@pytest.mark.parametrize("url", PRIVATE_URLS)
async def test_import_entries_reject_non_public_urls_before_requests_or_writes(monkeypatch, entry, url):
    from app.services import resource_discovery
    from app.services.external_capabilities import mcp_source_adapter
    from app.tools.handlers.mcp import import_mcp_server

    async def tenant(_agent_id):
        return uuid4()

    def forbidden(*args, **kwargs):
        raise AssertionError("Rejected imports must not request or persist anything")

    monkeypatch.setattr(resource_discovery, "_resolve_agent_tenant_id", tenant)
    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", tenant)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(resource_discovery, "tenant_scoped_session", forbidden)
    if entry in {"bot", "command"}:
        arguments = {"server_id": "Public docs", "config": {"mcp_url": url}}
        if entry == "command":
            from app.api.commands import _normalize_tool_command
            tool_name, arguments = _normalize_tool_command("mcp", {"action": "import_server", **arguments})
            assert tool_name == "import_mcp_server"
        result = await import_mcp_server(uuid4(), arguments)
        assert "可公开访问的 HTTP 或 HTTPS 地址" in result
        assert '"error_class": "bad_arguments"' in result
        assert "当前没有权限完成这次操作" not in result
    elif entry == "admin":
        with pytest.raises(ValueError, match="可公开访问的 HTTP 或 HTTPS 地址"):
            await mcp_source_adapter.stage_external_mcp_server_review(SimpleNamespace(add=forbidden), uuid4(),
                created_by_user_id=uuid4(), server_id=None, mcp_url=url, server_name="Public docs", config=None)
    else:
        with pytest.raises(MCPAuthzError, match="可公开访问的 HTTP 或 HTTPS 地址"):
            await resource_discovery.import_mcp_direct(url, uuid4())


@pytest.mark.parametrize("entry", ["/enterprise/mcp-servers/import", "/tools", "/tools/test-mcp"])
@pytest.mark.parametrize("url", PRIVATE_URLS)
async def test_admin_http_entries_return_plain_rejection_without_fetch_or_persistence(monkeypatch, entry, url):
    from fastapi import FastAPI
    from app.api.mcp_servers import router as servers_router
    from app.api.tools import router as tools_router
    from app.core.security import get_current_admin
    from app.database import get_db

    class NoWrites:
        def __getattr__(self, name):
            raise AssertionError("Rejected MCP URL must not write: " + name)

    app = FastAPI()
    app.include_router(servers_router)
    app.include_router(tools_router)
    app.dependency_overrides[get_current_admin] = lambda: SimpleNamespace(id=uuid4(), tenant_id=uuid4(), role="org_admin")
    app.dependency_overrides[get_db] = lambda: NoWrites()
    original = httpx.AsyncClient
    requests = []
    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"result": {"tools": []}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**{**kwargs, "transport": httpx.MockTransport(respond)}))
    payload = ({"mcp_url": url} if entry.endswith("/import") else {"server_url": url} if entry.endswith("test-mcp")
        else {"name": "mcp__public__read", "display_name": "Read", "type": "mcp", "mcp_server_url": url})
    async with original(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://g4a.test") as client:
        response = await client.post(entry, json=payload)
    assert response.status_code == 400, response.text
    assert "可公开访问的 HTTP 或 HTTPS 地址" in response.text
    assert requests == []


@pytest.fixture
def public_transport(monkeypatch):
    async def resolve(_hostname, _port):
        return ("93.184.216.34",)

    monkeypatch.setattr(governed_egress, "resolve_public_addresses", resolve)
    original = httpx.AsyncClient

    def install(handler):
        def factory(**kwargs):
            kwargs.pop("transport", None)
            return original(transport=httpx.MockTransport(handler), **kwargs)
        monkeypatch.setattr(httpx, "AsyncClient", factory)
    return install


@pytest.mark.parametrize("url", PRIVATE_URLS)
async def test_stored_mcp_endpoint_is_checked_before_fetch(public_transport, url):
    from app.services.mcp_client import MCPClient

    requests = []
    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"result": {"tools": []}})
    public_transport(respond)
    with pytest.raises(MCPAuthzError, match="可公开访问的 HTTP 或 HTTPS 地址"):
        await MCPClient(url).list_tools()
    assert requests == []


@pytest.mark.parametrize("transport", ["streamable", "sse"])
@pytest.mark.parametrize("destination", PRIVATE_URLS)
async def test_mcp_redirect_never_reaches_private_address(public_transport, transport, destination):
    from app.services.mcp_client import MCPClient

    requests = []
    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(307, headers={"Location": destination})
    public_transport(respond)
    client = MCPClient("https://public-mcp.example/mcp")
    client._transport = transport
    with pytest.raises(MCPAuthzError, match="可公开访问的 HTTP 或 HTTPS 地址"):
        await client.list_tools()
    assert requests == ["https://public-mcp.example/mcp" + ("/sse" if transport == "sse" else "")]


async def test_mcp_rejects_private_dns_after_import(public_transport, monkeypatch):
    from app.services.mcp_client import MCPClient

    public_transport(lambda request: pytest.fail("Private DNS must be rejected before HTTP"))
    async def rebound(_hostname, _port):
        return ("93.184.216.34", "10.0.0.2")
    monkeypatch.setattr(governed_egress, "resolve_public_addresses", rebound)
    with pytest.raises(MCPAuthzError, match="可公开访问的 HTTP 或 HTTPS 地址"):
        await MCPClient("https://public-mcp.example/mcp").list_tools()


async def test_keyless_mcp_tools_remain_available_to_policy_authorized_assistants(monkeypatch):
    from app.services import agent_tools, resource_discovery

    async def configs(names):
        return {name: {} for name in names}
    async def no_key(*args):
        return ""
    monkeypatch.setattr(agent_tools, "_get_tool_configs", configs)
    monkeypatch.setattr(resource_discovery, "_get_smithery_api_key", no_key)
    monkeypatch.setattr(resource_discovery, "_get_modelscope_api_token", no_key)
    tools = [{"function": {"name": name}} for name in ("discover_resources", "import_mcp_server")]
    assert await agent_tools._filter_unavailable_tools(uuid4(), tools) == tools
