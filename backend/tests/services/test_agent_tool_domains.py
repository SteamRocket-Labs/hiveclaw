from __future__ import annotations

import uuid

import pytest


def test_workspace_tool_functions_are_sourced_from_workspace_domain():
    from app.services import agent_tools

    assert agent_tools._list_files.__module__ == "app.services.agent_tool_domains.workspace"
    assert agent_tools._read_file.__module__ == "app.services.agent_tool_domains.workspace"
    assert agent_tools._load_skill.__module__ == "app.services.agent_tool_domains.workspace"


def test_web_and_mcp_tool_functions_are_sourced_from_web_mcp_domain():
    from app.services import agent_tools

    assert agent_tools._web_search.__module__ == "app.services.agent_tool_domains.web_mcp"
    assert agent_tools._discover_resources.__module__ == "app.services.agent_tool_domains.web_mcp"
    assert agent_tools._import_mcp_server.__module__ == "app.services.agent_tool_domains.web_mcp"


@pytest.mark.asyncio
async def test_agent_mcp_import_uses_agent_scoped_server_registration(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    agent_id = uuid.uuid4()
    captured = {}

    async def fake_import_for_agent(
        target_agent_id, *, server_id=None, mcp_url=None, server_name=None, config=None, reauthorize=False
    ):
        captured.update(
            agent_id=target_agent_id,
            server_id=server_id,
            mcp_url=mcp_url,
            server_name=server_name,
            config=config,
            reauthorize=reauthorize,
        )
        return "registered server"

    monkeypatch.setattr(web_mcp, "import_mcp_for_agent_and_register", fake_import_for_agent)

    result = await web_mcp._import_mcp_server(
        agent_id,
        {"server_id": "github", "config": {"smithery_api_key": "sk-test"}, "reauthorize": True},
    )

    assert result == "registered server"
    assert captured == {
        "agent_id": agent_id,
        "server_id": "github",
        "mcp_url": None,
        "server_name": None,
        "config": {"smithery_api_key": "sk-test"},
        "reauthorize": True,
    }
