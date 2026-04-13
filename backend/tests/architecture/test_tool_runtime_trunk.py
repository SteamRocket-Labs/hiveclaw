from __future__ import annotations

from pathlib import Path


def _python_sources() -> dict[str, str]:
    project_root = Path(__file__).resolve().parents[3]
    app_root = project_root / "backend/app"
    return {
        str(path.relative_to(project_root)): path.read_text(encoding="utf-8")
        for path in app_root.rglob("*.py")
    }


def test_tool_runtime_trunk_keeps_metadata_and_execution_single_pathed() -> None:
    sources = _python_sources()

    execute_tool_refs = sorted(path for path, source in sources.items() if "execute_tool(" in source)

    assert execute_tool_refs == [
        "backend/app/kernel/engine.py",
        "backend/app/runtime/invoker.py",
        "backend/app/services/agent_tool_domains/messaging.py",
        "backend/app/services/agent_tools.py",
        "backend/app/services/heartbeat.py",
    ]

    agent_tools_source = sources["backend/app/services/agent_tools.py"]
    invoker_source = sources["backend/app/runtime/invoker.py"]
    heartbeat_source = sources["backend/app/services/heartbeat.py"]
    messaging_source = sources["backend/app/services/agent_tool_domains/messaging.py"]

    assert "return ToolRegistry.from_openai_tools(collected.openai_tools).to_openai_tools()" in agent_tools_source
    assert "ToolRegistry.from_openai_tools(result).to_openai_tools()" in agent_tools_source
    assert "ToolRegistry.from_openai_tools(fallback).to_openai_tools()" in agent_tools_source
    assert "return await _get_tool_runtime_service().execute(" in agent_tools_source
    assert "return await _get_tool_runtime_service().execute_direct(" in agent_tools_source
    assert "if request.tool_executor:" in invoker_source
    assert "return await execute_tool(tool_name, args, agent_id, creator_id)" in heartbeat_source
    assert "tool_result = await execute_tool(tool_name, tool_args, target_agent_id, owner_id)" in messaging_source

    # direct fallback 只能兜未知工具 / MCP passthrough，不能再手写第一类工具分发。
    duplicate_direct_branches = [
        'if tool_name == "delete_file":',
        'if tool_name == "write_file":',
        'if tool_name == "execute_code":',
        'if tool_name == "run_command":',
        'if tool_name == "web_fetch":',
        'if tool_name == "web_search":',
        'if tool_name == "firecrawl_fetch":',
        'if tool_name == "xcrawl_scrape":',
        'if tool_name == "send_feishu_message":',
        'if tool_name == "send_channel_message":',
        'if tool_name == "send_message_to_agent":',
        'if tool_name == "delegate_to_agent":',
        'if tool_name == "check_async_task":',
        'if tool_name == "cancel_async_task":',
        'if tool_name == "list_async_tasks":',
        'if tool_name == "get_current_time":',
    ]
    for branch in duplicate_direct_branches:
        assert branch not in agent_tools_source
