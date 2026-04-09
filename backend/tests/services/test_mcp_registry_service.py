from __future__ import annotations


def test_group_mcp_registry_rows_builds_stable_server_registry():
    from app.services.mcp_registry_service import build_mcp_server_registry

    rows = [
        {
            "tool_id": "tool-1",
            "tool_name": "mcp_github_issue_search",
            "display_name": "GitHub: issue_search",
            "mcp_server_name": "GitHub",
            "mcp_server_url": "https://github.run.tools",
            "agent_id": "agent-1",
            "agent_name": "Ops Agent",
        },
        {
            "tool_id": "tool-2",
            "tool_name": "mcp_github_repo_read",
            "display_name": "GitHub: repo_read",
            "mcp_server_name": "GitHub",
            "mcp_server_url": "https://github.run.tools",
            "agent_id": "agent-2",
            "agent_name": "Research Agent",
        },
    ]

    registry = build_mcp_server_registry(rows)

    assert registry == [{
        "server_key": "mcp_server:github",
        "server_name": "GitHub",
        "server_url": "https://github.run.tools",
        "tool_count": 2,
        "agent_count": 2,
        "tools": ["mcp_github_issue_search", "mcp_github_repo_read"],
        "agents": ["Ops Agent", "Research Agent"],
        "pack_name": "mcp_server:github",
    }]
