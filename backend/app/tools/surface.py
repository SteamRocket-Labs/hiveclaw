"""Canonical tool surface assembly and selection for runtime."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.services.pack_policy_service import get_tenant_pack_policies, is_pack_enabled
from app.services.agent_tool_domains.web_mcp import (
    _get_exa_api_key,
    _get_firecrawl_api_key,
    _get_xcrawl_api_key,
)

from .packs import make_mcp_server_pack_name, static_pack_names_for_tool
from .registry import ToolRegistry

logger = logging.getLogger(__name__)

_COLLECTED_TOOLS = None


def get_collected_tools():
    """Lazy-load collected tool metadata and executors from decorators."""
    global _COLLECTED_TOOLS
    if _COLLECTED_TOOLS is None:
        from app.tools.collector import collect_tools

        _COLLECTED_TOOLS = collect_tools()
    return _COLLECTED_TOOLS


# Minimal-by-default kernel tools. Everything else should be introduced
# explicitly via skills, channel capabilities, or MCP-linked expansion.
CORE_TOOL_NAMES = {
    "execute_code",
    "run_command",
    "list_files",
    "read_file",
    "write_file",
    "edit_file",
    "glob_search",
    "grep_search",
    "load_skill",
    "save_skill",
    "search_memory",
    "save_memory",
    "set_trigger",
    "list_triggers",
    "send_message_to_agent",
    "delegate_to_agent",
    "check_async_task",
    "cancel_async_task",
    "list_async_tasks",
    "get_current_time",
    "send_channel_message",
    "send_channel_file",
    "tool_search",
    "web_fetch",
}

_ALWAYS_INCLUDE_CORE = set(CORE_TOOL_NAMES)

_HR_TOOL_NAMES = {
    "create_digital_employee",
    "preview_agent_blueprint",
    "discover_resources",
    "search_clawhub",
    "web_search",
    "firecrawl_fetch",
    "xcrawl_scrape",
    "execute_code",
}

_FEISHU_TOOL_NAMES = {
    "send_feishu_message",
    "feishu_user_search",
    "feishu_wiki_list",
    "feishu_doc_read",
    "feishu_doc_delete",
    "feishu_sheet_info",
    "feishu_sheet_read",
    "feishu_base_app_create",
    "feishu_base_field_list",
    "feishu_base_field_create",
    "feishu_base_table_list",
    "feishu_base_record_list",
    "feishu_base_record_delete",
    "feishu_base_record_upload_attachment",
    "feishu_base_record_upsert",
    "feishu_approval_create",
    "feishu_approval_query",
    "feishu_approval_get",
    "feishu_task_comment",
    "feishu_task_complete",
    "feishu_task_create",
    "feishu_task_list",
    "feishu_doc_create",
    "feishu_doc_append",
    "feishu_doc_share",
    "feishu_calendar_list",
    "feishu_calendar_create",
    "feishu_calendar_update",
    "feishu_calendar_delete",
}

_FEISHU_OFFICE_TOOL_NAMES = {
    "feishu_wiki_list",
    "feishu_doc_read",
    "feishu_base_app_create",
    "feishu_sheet_info",
    "feishu_sheet_read",
    "feishu_base_field_list",
    "feishu_base_field_create",
    "feishu_base_table_list",
    "feishu_base_record_list",
    "feishu_base_record_delete",
    "feishu_base_record_upload_attachment",
    "feishu_base_record_upsert",
    "feishu_task_comment",
    "feishu_task_complete",
    "feishu_task_create",
    "feishu_task_list",
}

_FEISHU_CLI_ONLY_TOOL_NAMES: set[str] = set()
_always_core_tools: list[dict] | None = None
_feishu_tools: list[dict] | None = None
_hr_tools: list[dict] | None = None


def get_combined_openai_tools() -> list[dict]:
    """Return the canonical OpenAI tool surface collected from decorators."""
    collected = get_collected_tools()
    category_overrides = {
        item["name"]: item["category"]
        for item in getattr(collected, "seed_list", [])
        if item.get("name") and item.get("category")
    }
    return ToolRegistry.from_openai_tools(
        collected.openai_tools,
        category_overrides=category_overrides,
    ).to_openai_tools()


async def _provider_available_tools(agent_id: uuid.UUID | None = None) -> set[str]:
    """Return provider-backed tools that are actually configured."""
    available: set[str] = set()

    exa_key = await _get_exa_api_key()
    firecrawl_key = await _get_firecrawl_api_key()
    xcrawl_key = await _get_xcrawl_api_key()
    if exa_key:
        available.add("web_search")
    if firecrawl_key:
        available.add("firecrawl_fetch")
    if xcrawl_key:
        available.add("xcrawl_scrape")

    try:
        from app.services.resource_discovery import _get_modelscope_api_token, _get_smithery_api_key

        smithery_key = await _get_smithery_api_key(agent_id)
        modelscope_token = await _get_modelscope_api_token()
        if smithery_key or modelscope_token:
            available |= {"discover_resources", "import_mcp_server"}
    except Exception:
        logger.debug("[Tools] Provider availability lookup failed", exc_info=True)

    return available


async def _filter_unavailable_tools(agent_id: uuid.UUID, tools: list[dict]) -> list[dict]:
    """Hide externally-backed tools that are not configured in production."""
    provider_backed = {"firecrawl_fetch", "xcrawl_scrape", "discover_resources", "import_mcp_server"}
    available = await _provider_available_tools(agent_id)
    return [
        tool
        for tool in tools
        if tool["function"]["name"] not in provider_backed or tool["function"]["name"] in available
    ]


def _get_always_core_tools() -> list[dict]:
    global _always_core_tools
    if _always_core_tools is None:
        all_tools = get_combined_openai_tools()
        _always_core_tools = [t for t in all_tools if t["function"]["name"] in _ALWAYS_INCLUDE_CORE]
    return _always_core_tools


def _get_feishu_tools() -> list[dict]:
    global _feishu_tools
    if _feishu_tools is None:
        all_tools = get_combined_openai_tools()
        _feishu_tools = [t for t in all_tools if t["function"]["name"] in _FEISHU_TOOL_NAMES]
    return _feishu_tools


def _filter_feishu_tools_for_access(
    tools: list[dict],
    *,
    has_feishu_channel: bool,
    has_feishu_office_access: bool,
    has_feishu_cli_access: bool,
) -> list[dict]:
    """Select Feishu tools according to channel vs CLI-backed office access."""
    filtered: list[dict] = []
    for tool in tools:
        name = tool["function"]["name"]
        if name in _FEISHU_CLI_ONLY_TOOL_NAMES:
            if has_feishu_cli_access:
                filtered.append(tool)
            continue
        if name in _FEISHU_OFFICE_TOOL_NAMES:
            if has_feishu_office_access:
                filtered.append(tool)
            continue
        if has_feishu_channel:
            filtered.append(tool)
    return filtered


def _get_hr_tools() -> list[dict]:
    global _hr_tools
    if _hr_tools is None:
        all_tools = get_combined_openai_tools()
        _hr_tools = [t for t in all_tools if t["function"]["name"] in _HR_TOOL_NAMES]
    return _hr_tools


async def _agent_has_feishu(agent_id: uuid.UUID) -> bool:
    """Check if agent has a configured Feishu channel."""
    try:
        from app.models.channel_config import ChannelConfig

        async with async_session() as db:
            result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "feishu",
                    ChannelConfig.is_configured,
                )
            )
            return result.scalar_one_or_none() is not None
    except Exception:
        return False


async def _agent_has_feishu_office_access(agent_id: uuid.UUID) -> bool:
    """Office read access is available via channel creds or optional lark-cli auth."""
    if await _agent_has_feishu(agent_id):
        return True
    return await _agent_has_feishu_cli_access()


async def _agent_has_feishu_cli_access() -> bool:
    """CLI-backed office access is available when lark-cli is enabled and authenticated."""
    from app.services.agent_tool_domains.feishu_cli import _feishu_cli_available

    return await _feishu_cli_available()


async def get_agent_tools_for_llm(
    agent_id: uuid.UUID,
    core_only: bool = False,
    requested_names: list[str] | None = None,
) -> list[dict]:
    """Load enabled tools for an agent from DB in OpenAI function-calling format."""
    has_feishu_channel = await _agent_has_feishu(agent_id)
    has_feishu_cli_access = await _agent_has_feishu_cli_access()
    has_feishu_office_access = has_feishu_channel or has_feishu_cli_access
    requested_set = set(requested_names or [])
    if requested_set:
        requested_set |= CORE_TOOL_NAMES

    try:
        from app.models.tool import AgentTool, Tool

        async with async_session() as db:
            agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            is_system_agent = agent is not None and getattr(agent, "agent_class", None) == "internal_system"
            core_tools = _get_always_core_tools()
            if is_system_agent:
                core_tools = [t for t in core_tools if t["function"]["name"] != "tool_search"]
            always_tools = (
                core_tools
                + _filter_feishu_tools_for_access(
                    _get_feishu_tools(),
                    has_feishu_channel=has_feishu_channel,
                    has_feishu_office_access=has_feishu_office_access,
                    has_feishu_cli_access=has_feishu_cli_access,
                )
                + (_get_hr_tools() if is_system_agent else [])
            )
            pack_policies = await get_tenant_pack_policies(db, getattr(agent, "tenant_id", None))

            all_tools_result = await db.execute(select(Tool).where(Tool.enabled))
            all_tools = all_tools_result.scalars().all()

            assignments_result = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))
            assignments = {str(at.tool_id): at for at in assignments_result.scalars().all()}

            result: list[dict] = []
            db_tool_names: set[str] = set()
            category_overrides: dict[str, str] = {}
            for tool in all_tools:
                tool_id = str(tool.id)
                assignment = assignments.get(tool_id)
                enabled = assignment.enabled if assignment else tool.is_default
                if not enabled:
                    continue

                if tool.category == "feishu":
                    if tool.name in _FEISHU_CLI_ONLY_TOOL_NAMES and not has_feishu_cli_access:
                        continue
                    if tool.name in _FEISHU_OFFICE_TOOL_NAMES and not has_feishu_office_access:
                        continue
                    if (
                        tool.name not in _FEISHU_OFFICE_TOOL_NAMES
                        and tool.name not in _FEISHU_CLI_ONLY_TOOL_NAMES
                        and not has_feishu_channel
                    ):
                        continue

                static_packs = set(static_pack_names_for_tool(tool.name))
                if tool.type == "mcp":
                    static_packs.add(make_mcp_server_pack_name(tool.mcp_server_name, tool.mcp_server_url))
                if static_packs and not any(is_pack_enabled(pack_policies, pack_name) for pack_name in static_packs):
                    continue

                raw_params = tool.parameters_schema or {"type": "object", "properties": {}}
                result.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": raw_params,
                        },
                    }
                )
                db_tool_names.add(tool.name)
                category_overrides[tool.name] = tool.category

            if result:
                for tool in always_tools:
                    if tool["function"]["name"] not in db_tool_names:
                        result.append(tool)
                if core_only:
                    keep = CORE_TOOL_NAMES | (_HR_TOOL_NAMES if is_system_agent else set())
                    result = [tool for tool in result if tool["function"]["name"] in keep]
                elif requested_set:
                    result = [tool for tool in result if tool["function"]["name"] in requested_set]
                result = await _filter_unavailable_tools(agent_id, result)
                return ToolRegistry.from_openai_tools(
                    result,
                    category_overrides=category_overrides,
                ).to_openai_tools()
    except Exception as exc:
        logger.error(f"[Tools] DB load failed, using fallback: {exc}")

    fallback = get_combined_openai_tools()
    allowed_feishu_names = {
        tool["function"]["name"]
        for tool in _filter_feishu_tools_for_access(
            [tool for tool in fallback if tool["function"]["name"] in _FEISHU_TOOL_NAMES],
            has_feishu_channel=has_feishu_channel,
            has_feishu_office_access=has_feishu_office_access,
            has_feishu_cli_access=has_feishu_cli_access,
        )
    }
    fallback = [
        tool
        for tool in fallback
        if tool["function"]["name"] not in _FEISHU_TOOL_NAMES
        or tool["function"]["name"] in allowed_feishu_names
    ]
    if core_only:
        fallback = [tool for tool in fallback if tool["function"]["name"] in CORE_TOOL_NAMES]
    elif requested_set:
        fallback = [tool for tool in fallback if tool["function"]["name"] in requested_set]
    fallback = await _filter_unavailable_tools(agent_id, fallback)
    return ToolRegistry.from_openai_tools(fallback).to_openai_tools()
