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
        "backend/app/tools/execution_entry.py",
    ]

    agent_tools_source = sources["backend/app/services/agent_tools.py"]
    invoker_source = sources["backend/app/runtime/invoker.py"]
    heartbeat_source = sources["backend/app/services/heartbeat.py"]
    messaging_source = sources["backend/app/services/agent_tool_domains/messaging.py"]
    registry_source = sources["backend/app/tools/registry.py"]
    execution_entry_source = sources["backend/app/tools/execution_entry.py"]
    communication_handler_source = sources["backend/app/tools/handlers/communication.py"]
    filesystem_handler_source = sources["backend/app/tools/handlers/filesystem.py"]
    plaza_handler_source = sources["backend/app/tools/handlers/plaza.py"]
    triggers_handler_source = sources["backend/app/tools/handlers/triggers.py"]
    email_handler_source = sources["backend/app/tools/handlers/email.py"]
    feishu_handler_source = sources["backend/app/tools/handlers/feishu.py"]
    tools_api_source = sources["backend/app/api/tools.py"]
    channel_delivery_domain_source = sources["backend/app/services/agent_tool_domains/channel_delivery.py"]
    tool_surface_source = sources["backend/app/tools/surface.py"]

    assert "return ToolRegistry.from_openai_tools(" in tool_surface_source
    assert tool_surface_source.count("category_overrides=category_overrides") == 2
    assert "ToolRegistry.from_openai_tools(fallback).to_openai_tools()" in tool_surface_source
    assert "category=infer_category(name)" not in registry_source
    assert "def get_combined_openai_tools()" in tool_surface_source
    assert "async def get_agent_tools_for_llm(" in tool_surface_source
    assert "async def _provider_available_tools(" in tool_surface_source
    assert "async def execute_tool_direct(" in execution_entry_source
    assert "async def execute_tool(" in execution_entry_source
    assert "async def execute_tool_inner(" in execution_entry_source
    assert "_TOOL_EXECUTION_REGISTRY = ToolExecutionRegistry()" in execution_entry_source
    assert "_TOOL_RUNTIME_SERVICE: ToolRuntimeService | None = None" in execution_entry_source
    assert "return await _execute_tool_entry(" in agent_tools_source
    assert "return await _execute_tool_direct_entry(" in agent_tools_source
    assert "return await _execute_tool_inner_entry(" in agent_tools_source
    assert "return _get_combined_openai_tools()" in agent_tools_source
    assert "return await _get_agent_tools_for_llm(" in agent_tools_source
    assert "if request.tool_executor:" in invoker_source
    assert "return await execute_tool(tool_name, args, agent_id, creator_id)" in heartbeat_source
    assert "tool_result = await execute_tool(tool_name, tool_args, target_agent_id, owner_id)" in messaging_source
    assert "_STATIC_READ_ONLY_TOOL_NAMES" not in registry_source
    assert "_STATIC_PARALLEL_SAFE_TOOL_NAMES" not in registry_source
    assert "from app.services.agent_tools import _send_feishu_message" not in communication_handler_source
    assert "from app.services.agent_tools import _send_web_message" not in communication_handler_source
    assert "from app.services.agent_tools import _send_message_to_agent" not in communication_handler_source
    assert "from app.services.agent_tools import _delegate_to_agent_async" not in communication_handler_source
    assert "from app.services.agent_tools import _check_async_task" not in communication_handler_source
    assert "from app.services.agent_tools import _cancel_async_task" not in communication_handler_source
    assert "from app.services.agent_tools import _list_async_tasks" not in communication_handler_source
    assert "from app.services.agent_tools import _get_current_time" not in communication_handler_source
    assert "from app.services.agent_tools import _upload_image" not in communication_handler_source
    assert "from app.services.agent_tools import _send_channel_message" not in communication_handler_source
    assert "from app.services.agent_tools import _send_channel_file" not in communication_handler_source
    assert "from app.services.agent_tool_domains.channel_delivery import _send_channel_message" in communication_handler_source
    assert "from app.services.agent_tool_domains.channel_delivery import _send_channel_file" in communication_handler_source
    assert "from app.services.agent_tools import _execute_code" not in filesystem_handler_source
    assert "from app.services.agent_tools import _run_command" not in filesystem_handler_source
    assert "from app.services.agent_tools import _plaza_get_new_posts" not in plaza_handler_source
    assert "from app.services.agent_tools import _plaza_create_post" not in plaza_handler_source
    assert "from app.services.agent_tools import _plaza_add_comment" not in plaza_handler_source
    assert "from app.services.agent_tools import _handle_set_trigger" not in triggers_handler_source
    assert "from app.services.agent_tools import _handle_update_trigger" not in triggers_handler_source
    assert "from app.services.agent_tools import _handle_cancel_trigger" not in triggers_handler_source
    assert "from app.services.agent_tools import _handle_list_triggers" not in triggers_handler_source
    assert "from app.services.agent_tools import _handle_email_tool" not in email_handler_source
    assert "from app.services.agent_tools import _feishu_" not in feishu_handler_source
    assert "channel_file_sender: ContextVar" not in agent_tools_source
    assert "channel_web_agent_id: ContextVar" not in agent_tools_source
    assert "channel_feishu_sender_open_id: ContextVar" not in agent_tools_source
    assert "async def _send_channel_file(" not in agent_tools_source
    assert "async def _send_channel_message(" not in agent_tools_source
    assert "async def _provider_available_tools(" not in agent_tools_source
    assert "async def _filter_unavailable_tools(" not in agent_tools_source
    assert "def _get_always_core_tools(" not in agent_tools_source
    assert "def _get_feishu_tools(" not in agent_tools_source
    assert "def _get_hr_tools(" not in agent_tools_source
    assert "def _filter_feishu_tools_for_access(" not in agent_tools_source
    assert "async def _agent_has_feishu(" not in agent_tools_source
    assert "async def _agent_has_feishu_office_access(" not in agent_tools_source
    assert "async def _agent_has_feishu_cli_access(" not in agent_tools_source
    assert "from app.services.agent_tools import _agent_has_feishu" not in tools_api_source
    assert "from app.tools.surface import" in tools_api_source
    assert "_agent_has_feishu" in tools_api_source
    assert "_agent_has_feishu_cli_access" in tools_api_source
    assert "_agent_has_feishu_office_access" in tools_api_source
    assert "_TOOL_EXECUTION_REGISTRY = ToolExecutionRegistry()" not in agent_tools_source
    assert "_TOOL_RUNTIME_SERVICE: ToolRuntimeService | None = None" not in agent_tools_source
    assert "# ─── Domain module re-exports" not in agent_tools_source
    assert "from app.services.agent_tool_domains.workspace import (" not in agent_tools_source
    assert "from app.services.agent_tool_domains.messaging import (" not in agent_tools_source
    assert "from app.services.agent_tool_domains.web_mcp import (" not in agent_tools_source
    assert "channel_file_sender: ContextVar" in channel_delivery_domain_source
    assert "channel_web_agent_id: ContextVar" in channel_delivery_domain_source
    assert "channel_feishu_sender_open_id: ContextVar" in channel_delivery_domain_source
    assert "async def _send_channel_file(" in channel_delivery_domain_source
    assert "async def _send_channel_message(" in channel_delivery_domain_source

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
