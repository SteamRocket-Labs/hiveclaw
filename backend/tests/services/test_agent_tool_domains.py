from __future__ import annotations


def test_workspace_tool_functions_are_sourced_from_workspace_domain():
    from app.services.agent_tool_domains import workspace as workspace_domain

    assert workspace_domain._list_files.__module__ == "app.services.agent_tool_domains.workspace"
    assert workspace_domain._read_file.__module__ == "app.services.agent_tool_domains.workspace"
    assert workspace_domain._load_skill.__module__ == "app.services.agent_tool_domains.workspace"


def test_web_and_mcp_tool_functions_are_sourced_from_web_mcp_domain():
    from app.services.agent_tool_domains import web_mcp as web_mcp_domain

    assert web_mcp_domain._web_search.__module__ == "app.services.agent_tool_domains.web_mcp"
    assert web_mcp_domain._discover_resources.__module__ == "app.services.agent_tool_domains.web_mcp"
    assert web_mcp_domain._import_mcp_server.__module__ == "app.services.agent_tool_domains.web_mcp"
