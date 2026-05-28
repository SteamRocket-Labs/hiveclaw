from __future__ import annotations


def _tool_names(tools: list[dict]) -> list[str]:
    return [tool["function"]["name"] for tool in tools]


def test_tool_registry_round_trips_collected_openai_tools():
    from app.services.agent_tools import get_combined_openai_tools
    from app.tools.registry import ToolRegistry

    all_tools = get_combined_openai_tools()
    registry = ToolRegistry.from_openai_tools(all_tools)

    assert "load_skill" in registry.names()
    assert "save_skill" in registry.names()
    assert "set_trigger" in registry.names()
    assert "send_feishu_message" in registry.names()

    tool = registry.get("send_feishu_message")
    assert tool.name == "send_feishu_message"
    assert tool.parameters["required"] == ["message"]

    llm_tools = registry.to_openai_tools(names=["load_skill", "set_trigger"])
    assert _tool_names(llm_tools) == ["load_skill", "set_trigger"]


def test_tool_catalog_groups_tools_into_readable_sections():
    from app.tools.catalog import ToolCatalog
    from app.tools.registry import ToolRegistry

    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read file content",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": "Load a skill",
                "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_trigger",
                "description": "Create a trigger",
                "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
            },
        },
    ]

    registry = ToolRegistry.from_openai_tools(tools)
    catalog = ToolCatalog(registry).render()

    assert "## Available Tools" in catalog
    assert "### File System" in catalog
    assert "### Skills" in catalog
    assert "### Scheduled" in catalog
    assert "- `read_file`:" in catalog


def test_minimal_kernel_tool_set_stays_small_and_explicit():
    from app.services.agent_tools import CORE_TOOL_NAMES

    assert CORE_TOOL_NAMES == {
        "execute_code",
        "run_command",
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "glob_search",
        "grep_search",
        "fs_read",
        "fs_write",
        "fs_list",
        "load_skill",
        "save_skill",
        "search_memory",
        "load_memory",
        "save_memory",
        "list_objectives",
        "propose_objective",
        "update_objective",
        "complete_objective",
        "set_trigger",
        "update_trigger",
        "cancel_trigger",
        "list_triggers",
        "send_message_to_agent",
        "delegate_to_agent",
        "check_async_task",
        "cancel_async_task",
        "list_async_tasks",
        "get_current_time",
        "send_channel_file",
        "send_channel_message",
        "tool_search",
        "web_fetch",
    }
    assert "web_search" not in CORE_TOOL_NAMES
