"""Regression cases from the D9s second-round credential-boundary review."""

import ast
import base64
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, quote_plus, urlparse
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest


@pytest.mark.parametrize("entry", ["streamable", "sse_connect", "sse_request"])
async def test_mcp_redirects_scope_authorization_to_configured_origin(monkeypatch, entry):
    from app.services.mcp_client import MCPClient

    client_type = httpx.AsyncClient
    for server_url, destination in (
        ("http://shared.example/sse", "https://shared.example/sse"),
        ("http://shared.example/sse", "http://shared.example/redirected"),
        ("http://例子.测试/sse", "https://例子.测试/sse"),
        ("http://例子.测试/sse", "http://例子.测试/redirected"),
    ):
        requests = []

        async def provider(request):
            requests.append(request)
            if request.url == httpx.URL(server_url):
                return httpx.Response(302, headers={"location": str(httpx.URL(destination))})
            if entry != "streamable" and request.method == "GET":
                return httpx.Response(200, text="event: endpoint\ndata: /messages\n\n",
                    headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"result": {"tools": []}})

        monkeypatch.setattr("app.services.mcp_client.httpx.AsyncClient",
            lambda **kwargs: client_type(transport=httpx.MockTransport(provider), **kwargs))
        client = MCPClient(server_url, api_key="D9sRound2SyntheticBearer")
        if entry == "streamable":
            await client._streamable_request("tools/list")
        elif entry == "sse_connect":
            await client._sse_connect()
        else:
            await client._sse_request("tools/list")
        first_hops = [r for r in requests if r.url == httpx.URL(server_url)]
        assert first_hops and all(r.headers["Authorization"] == "Bearer D9sRound2SyntheticBearer" for r in first_hops)
        redirected = [r for r in requests if r.url == httpx.URL(destination)]
        assert redirected
        if destination.startswith("https:"):
            assert all("Authorization" not in r.headers for r in redirected)
        else:
            assert all(r.headers["Authorization"] == "Bearer D9sRound2SyntheticBearer" for r in redirected)


async def _admin_test(monkeypatch, config, secret_config, provider):
    from app.api import custom_api_connectors as api
    from app.core.security import get_current_admin
    from app.database import get_db

    tool = SimpleNamespace(id=uuid4(), name="custom_api__round2__read", config=config)

    async def connector(*args):
        return tool

    async def secrets(*args, **kwargs):
        return secret_config

    monkeypatch.setattr(api, "_get_connector_or_404", connector)
    monkeypatch.setattr(api, "resolve_tool_config", secrets)
    monkeypatch.setattr("app.services.governed_egress.request_governed_http", provider)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_admin] = lambda: SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    app.dependency_overrides[get_db] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://hive.test") as client:
        return await client.post(f"/enterprise/custom-api-connectors/{tool.id}/test", json={"arguments": {}})


@pytest.mark.parametrize("echo", ["json_error", "json_success", "exception", "percent", "plus", "basic"])
async def test_admin_provider_echo_is_redacted_before_encoding(monkeypatch, echo):
    secret = 'D9sRound2Quoted"Password\\Opaque /+?#012345'
    username = "round2@example.test"
    basic = base64.b64encode(f"{username}:{secret}".encode()).decode()
    encoded = quote_plus(secret) if echo == "plus" else quote(secret, safe="")
    auth = {"scheme": "api_key", "in": "query", "name": "key"} if echo in {"percent", "plus"} else {"scheme": "basic"}
    secrets = {"api_key": secret} if echo in {"percent", "plus"} else {"basic_username": username, "basic_password": secret}
    forbidden = basic if echo == "basic" else encoded if echo in {"percent", "plus"} else secret

    async def provider(method, url, **kwargs):
        if echo == "exception":
            raise RuntimeError(f"Provider rejected {secret}")
        value = basic if echo == "basic" else f"https://api.example.test/?key={encoded}" if echo in {"percent", "plus"} else secret
        return httpx.Response(200 if echo == "json_success" else 422,
            json={"echo": value, "business_value": "preserve this"})

    response = await _admin_test(monkeypatch,
        {"base_url": "https://api.example.test", "auth": auth, "action": {"path": "/read"}}, secrets, provider)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert forbidden not in result
    assert json.dumps(forbidden)[1:-1] not in result
    assert "[REDACTED_SECRET]" in result
    assert response.json()["ok"] is (echo == "json_success")
    if echo != "exception":
        assert "preserve this" in result


@pytest.mark.parametrize("base_url", ["https://api.example:99999", "https://api.example:invalid"])
async def test_invalid_custom_api_port_returns_clean_422(monkeypatch, base_url):
    async def forbidden_provider(*args, **kwargs):
        raise AssertionError("An invalid target must not be sent")

    response = await _admin_test(monkeypatch,
        {"base_url": base_url, "action": {"path": "/read"}}, {}, forbidden_provider)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


@pytest.mark.parametrize("path,args,suffix", [
    ("/{a}/{b}", {"a": "", "b": "read"}, "/v1/read"),
    ("//evil.example/x", {}, "/v1/evil.example/x"),
])
async def test_custom_api_leading_slashes_keep_existing_same_origin_join(monkeypatch, path, args, suffix):
    from app.services.custom_api_connectors import execute_prepared_custom_api_request, prepare_custom_api_request

    sent = []

    async def wire(request):
        sent.append(request)
        return httpx.Response(200, text="Fixture completed")

    async def provider(method, url, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
            return await client.request(method, url, headers=kwargs["headers"])

    monkeypatch.setattr("app.services.governed_egress.request_governed_http", provider)
    prepared = prepare_custom_api_request(tool_name="custom_api__round2__read", arguments=args,
        tool_config={"base_url": "https://api.example/v1", "action": {"path": path}, "auth": {"scheme": "bearer"}},
        secret_config={"bearer_token": "D9sRound2SyntheticToken"})
    result = await execute_prepared_custom_api_request("custom_api__round2__read", prepared)
    assert "Fixture completed" in result
    assert len(sent) == 1
    assert sent[0].url.host == "api.example" and sent[0].url.path == suffix
    assert sent[0].headers["Authorization"] == "Bearer D9sRound2SyntheticToken"


async def test_mcp_origin_error_has_no_local_bridge_hint(monkeypatch):
    from app.services.mcp_client import MCPClient
    from app.tools.handlers.mcp import _render_mcp_authz_error

    async def provider(request):
        return httpx.Response(200, text="event: endpoint\ndata: https://other.example/messages\n\n",
            headers={"content-type": "text/event-stream"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr("app.services.mcp_client.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(provider), **kwargs))
    with pytest.raises(ValueError) as failure:
        await MCPClient("https://mcp.example/sse")._sse_connect()
    rendered = _render_mcp_authz_error("read", failure.value)
    assert "连接地址与配置不一致" in rendered
    assert "Local Bridge" not in rendered


@pytest.mark.parametrize("loader", ["redact_runtime_ingress_payload", "load_runtime_ingress_secret_boundary", "load_requester_exact_secret_boundary"])
def test_requester_is_an_explicit_required_keyword(loader):
    from app.services import credential_boundary_loader

    parameter = inspect.signature(getattr(credential_boundary_loader, loader)).parameters["requester_user_id"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_every_ingress_call_explicitly_selects_a_requester():
    app_root = Path(__file__).resolve().parents[2] / "app"
    missing = []
    for path in app_root.rglob("*.py"):
        source = path.read_text()
        if "redact_runtime_ingress_payload" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "redact_runtime_ingress_payload":
                if not any(k.arg == "requester_user_id" for k in node.keywords):
                    missing.append(f"{path.relative_to(app_root)}:{node.lineno}")
    assert not missing, missing


async def test_web_revision_uses_patch_caller_under_external_authority(monkeypatch):
    from app.services import session_human_input

    caller_id = uuid4()
    captured = []

    class Observed(Exception):
        pass

    class DB:
        async def get(self, *args):
            return SimpleNamespace(delivery_target_json={})

    async def locked(*args, **kwargs):
        return (SimpleNamespace(status="accepted", bound_round_id=None, revision=1, session_id=uuid4()),
            SimpleNamespace(state="accepted"), SimpleNamespace())

    async def redact(*args, **kwargs):
        captured.append(kwargs["requester_user_id"])
        raise Observed()

    monkeypatch.setattr(session_human_input, "_locked_input", locked)
    monkeypatch.setattr("app.services.credential_boundary_loader.redact_runtime_ingress_payload", redact)
    with pytest.raises(Observed):
        await session_human_input.revise_unbound_human_input(DB(),
            authority=SimpleNamespace(tenant_id=uuid4(), agent_id=uuid4(), principal_type="external_principal", principal_id=uuid4()),
            input_id=uuid4(), expected_revision=1, user=SimpleNamespace(id=caller_id),
            content_parts=[{"type": "text", "text": "Member revision"}])
    assert captured == [caller_id]
