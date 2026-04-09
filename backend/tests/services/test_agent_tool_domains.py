from __future__ import annotations


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
