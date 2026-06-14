from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_mcp_client_rejects_server_url_userinfo() -> None:
    from app.services.mcp_client import MCPClient

    with pytest.raises(ValueError, match="userinfo"):
        MCPClient("https://user:secret@mcp.example/mcp")


def test_mcp_client_rejects_oauth_token_query_passthrough() -> None:
    from app.services.mcp_client import MCPClient

    with pytest.raises(ValueError, match="token passthrough"):
        MCPClient("https://mcp.example/mcp?access_token=user-token")


def test_mcp_client_extracts_api_key_query_without_leaking_url_secret() -> None:
    from app.services.mcp_client import MCPClient

    client = MCPClient("https://mcp.example/mcp?apiKey=server-key&region=us")

    assert client.server_url == "https://mcp.example/mcp?region=us"
    assert client._headers()["Authorization"] == "Bearer server-key"


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        values = [self._value] if self._value else []
        return SimpleNamespace(all=lambda: values, first=lambda: values[0] if values else None)


class _RoutingSession:
    def __init__(self, tool):
        self.tool = tool

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, stmt):
        text = str(stmt)
        if "FROM tools" in text and "agent_tools" not in text:
            return _ScalarResult(self.tool)
        if "FROM agent_tools" in text:
            return _ScalarResult(None)
        return _ScalarResult(None)


@pytest.mark.asyncio
async def test_execute_mcp_tool_rejects_token_passthrough_config(monkeypatch) -> None:
    from app.services.agent_tool_domains import web_mcp

    agent_id = uuid4()
    tenant_id = uuid4()
    tool = SimpleNamespace(
        id=uuid4(),
        name="mcp_sensitive_search",
        type="mcp",
        enabled=True,
        is_default=True,
        tenant_id=None,
        config={"access_token": "user-oauth-token"},
        mcp_server_url="https://mcp.example/mcp",
        mcp_server_name="Sensitive MCP",
        mcp_tool_name="search",
    )

    async def _resolve(*_a, **_k):
        return tenant_id

    monkeypatch.setattr(web_mcp, "resolve_tenant_for_agent", _resolve, raising=False)
    monkeypatch.setattr(web_mcp, "tenant_scoped_session", lambda *_a, **_k: _RoutingSession(tool), raising=False)

    calls: list[str] = []

    class _FakeMCPClient:
        def __init__(self, *_args, **_kwargs):
            calls.append("init")

        async def call_tool(self, *_args, **_kwargs):
            calls.append("call")
            return "CALLED"

    monkeypatch.setattr("app.services.mcp_client.MCPClient", _FakeMCPClient)

    result = await web_mcp._execute_mcp_tool("mcp_sensitive_search", {"q": "x"}, agent_id=agent_id)

    assert calls == []
    assert "token passthrough" in result
