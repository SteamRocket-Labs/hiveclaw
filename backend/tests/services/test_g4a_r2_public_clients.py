"""Round-two MCP egress regressions at the actual HTTP client boundary."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.usefixtures("public_mcp_dns")


@pytest.fixture
def wire(monkeypatch):
    original = httpx.AsyncClient
    requests = []
    def install(respond):
        def handler(request):
            requests.append(request)
            return respond(request)
        def client(*args, **kwargs):
            if not isinstance(kwargs.get("transport"), httpx.ASGITransport):
                kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)
        monkeypatch.setattr(httpx, "AsyncClient", client)
        return requests
    return install


@pytest.mark.parametrize("endpoint", ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:6379/",
    "http://[::ffff:10.0.0.1]/token", "http://100.64.0.1/"])
async def test_refresh_refuses_stored_private_token_endpoint_before_sending(wire, endpoint):
    from app.services import mcp_oauth

    requests = wire(lambda _request: httpx.Response(200, json={"access_token": "synthetic-token"}))
    with pytest.raises(mcp_oauth.OAuthError):
        await mcp_oauth.refresh_access_token(token_endpoint=endpoint, client_id="synthetic",
            refresh_token="synthetic-refresh", client_secret="synthetic-secret")
    assert requests == []


@pytest.mark.parametrize("entry", ["registry_search", "connect_setup", "connect_call"])
async def test_fixed_host_mcp_clients_refuse_private_redirect_before_second_request(monkeypatch, wire, entry):
    from app.services import resource_discovery as discovery
    from app.services.agent_tool_domains.web_mcp import _execute_via_smithery_connect
    async def registry_key(_agent_id=None): return "synthetic-key"
    monkeypatch.setattr(discovery, "_get_smithery_api_key", registry_key)

    requests = wire(lambda _request: httpx.Response(302, headers={
        "Location": "http://169.254.169.254/latest/meta-data/"}))
    if entry == "registry_search":
        result = await discovery._search_smithery_api("public-documents", 5, "synthetic-key")
        assert result == []
    elif entry == "connect_setup":
        result = await discovery._ensure_smithery_connection("synthetic-key", "https://docs.example/mcp", "Documents")
        assert "error" in result
    else:
        result = await _execute_via_smithery_connect("https://docs.run.tools", "read", {}, {
            "smithery_api_key": "synthetic-key", "smithery_namespace": "synthetic", "smithery_connection_id": "documents"})
        assert "<tool_error>" in result
    assert len(requests) == 1
    assert requests[0].url.host in {"registry.smithery.ai", "api.smithery.ai"}
    assert requests[0].headers["Authorization"] == "Bearer synthetic-key"


async def test_namespaced_smithery_connect_key_without_deployment_url_is_not_a_fetch_url(monkeypatch, wire):
    from app.services import resource_discovery as discovery

    async def tenant(_agent): return uuid4()
    monkeypatch.setattr(discovery, "_resolve_agent_tenant_id", tenant)
    def respond(request):
        if request.url.path == "/servers":
            return httpx.Response(200, json={"servers": [{"qualifiedName": "@smithery-ai/github",
                "displayName": "GitHub", "remote": True}]})
        return httpx.Response(200, json={"tools": [{"name": "read", "inputSchema": {"type": "object"}}]})
    wire(respond)
    class ReachedConnect(Exception): pass
    async def connect(_key, target, _name):
        assert target == "https://@smithery-ai/github.run.tools"
        raise ReachedConnect
    monkeypatch.setattr(discovery, "_ensure_smithery_connection", connect)
    with pytest.raises(ReachedConnect):
        await discovery.import_mcp_from_smithery("@smithery-ai/github", uuid4(), {"smithery_api_key": "synthetic-key"})


@pytest.mark.parametrize("value", [123, False, ["https://public.example/mcp"], {"url": "https://public.example/mcp"}])
async def test_nested_non_string_mcp_url_is_the_existing_invalid_url_error(value):
    from app.services.external_capabilities.mcp_source_adapter import stage_external_mcp_server_review
    from app.services.mcp_authz import MCPPublicAddressError

    with pytest.raises(MCPPublicAddressError, match="请检查网址是否正确"):
        await stage_external_mcp_server_review(SimpleNamespace(), uuid4(), created_by_user_id=uuid4(),
            server_id="synthetic", mcp_url=None, server_name="Synthetic", config={"mcp_url": value})


@pytest.mark.parametrize("entry", ["mcp", "oauth"])
async def test_real_transport_connects_to_registered_public_pin(public_mcp_dns, entry):
    from app.services.mcp_client import MCPClient
    from app.services import mcp_oauth

    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            requests.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = {"access_token": "synthetic-token"} if entry == "oauth" else {"result": {"tools": [{"name": "read"}]}}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://public-mcp.example:{server.server_port}/token"
        if entry == "mcp":
            client = MCPClient(endpoint)
            client._session_id = "synthetic"
            assert await client.list_tools() == [{"name": "read", "description": "", "inputSchema": {}}]
        else:
            token = await mcp_oauth.refresh_access_token(token_endpoint=endpoint, client_id="synthetic", refresh_token="synthetic-refresh")
            assert token.access_token == "synthetic-token"
        assert requests == ["/token"]
        assert public_mcp_dns == ["93.184.216.34"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_admin_route_uses_clickable_chinese_catalog_labels():
    from pathlib import Path
    from app.runtime.prompts.mcp import MCP_ADMIN_ROUTE, IMPORT_MCP_SERVER_DESCRIPTION

    keys = ["enterprise.nav.companyConnections", "enterprise.tools.openMcpConnections", "enterprise.extensions.tabs.mcp",
        "enterprise.tools.extensionsAddons", "enterprise.tools.addMcpServer", "enterprise.tools.testConnection",
        "enterprise.tools.importAll", "agent.extensions.mcpServers", "enterprise.tools.reviewMcpMetadata",
        "enterprise.tools.approveFingerprint"]
    repo = Path(__file__).resolve().parents[3]
    catalog = json.loads((repo / "frontend/src/i18n/zh.json").read_text())
    expected = []
    for key in keys:
        value = catalog
        for segment in key.split("."): value = value[segment]
        expected.append(value)
    assert MCP_ADMIN_ROUTE.split(" → ") == expected
    assert catalog["enterprise"]["extensions"]["tabs"]["assets"] == "AI 资产"
    assert MCP_ADMIN_ROUTE in IMPORT_MCP_SERVER_DESCRIPTION
    skill = (repo / "backend/agent_template/skills/mcp-installer/SKILL.md").read_text()
    assert MCP_ADMIN_ROUTE in skill
    assert "AI 资产; switch to MCP 与插件" in skill
    assert "试试哪个" not in skill
