"""Agent tools — unified file-based tools that give digital employees
access to their own structured workspace.

Design principle: ONE set of file tools covers EVERYTHING.
The agent's workspace uses well-known paths:
  - tasks.json          → task list (auto-synced from DB)
  - soul.md             → personality definition
  - memory/*.md         → layered long-term memory (feedback / knowledge / strategies / blocked / user)
  - skills/             → skill definitions (markdown files)
  - workspace/          → general working files, reports, etc.

The agent reads/writes these files directly. No per-concept tools needed.
"""

import threading
import uuid
from typing import Awaitable, Callable

from app.tools import (
    ToolExecutionRegistry,
    ToolGovernanceResolver,
    ToolRuntimeService,
    run_tool_governance,
)
from app.tools.surface import (
    CORE_TOOL_NAMES as _CORE_TOOL_NAMES,
    _agent_has_feishu as _surface_agent_has_feishu,
    _agent_has_feishu_cli_access as _surface_agent_has_feishu_cli_access,
    _agent_has_feishu_office_access as _surface_agent_has_feishu_office_access,
    _filter_feishu_tools_for_access as _surface_filter_feishu_tools_for_access,
    get_agent_tools_for_llm as _get_agent_tools_for_llm,
    get_collected_tools as _surface_get_collected_tools,
    get_combined_openai_tools as _get_combined_openai_tools,
)

ToolEventCallback = Callable[[dict], Awaitable[None] | None]
CORE_TOOL_NAMES = _CORE_TOOL_NAMES

_TOOL_EXECUTION_REGISTRY = ToolExecutionRegistry()
_TOOL_EXECUTION_REGISTRY_INITIALIZED = False
_TOOL_RUNTIME_SERVICE: ToolRuntimeService | None = None
_REGISTRY_LOCK = threading.Lock()  # M-09: protect concurrent registry init


def _get_collected_tools():
    """Compatibility facade for collected tool metadata/executors."""
    return _surface_get_collected_tools()


def get_combined_openai_tools() -> list[dict]:
    """Compatibility facade for the canonical OpenAI tool surface."""
    return _get_combined_openai_tools()


def _ensure_tool_execution_registry() -> None:
    global _TOOL_EXECUTION_REGISTRY_INITIALIZED
    if _TOOL_EXECUTION_REGISTRY_INITIALIZED:
        return

    with _REGISTRY_LOCK:
        if _TOOL_EXECUTION_REGISTRY_INITIALIZED:
            return

        # Register @tool-decorated handlers (from tools/handlers/)
        collected = _get_collected_tools()
        for name, executor in collected.exec_registry._executors.items():
            _TOOL_EXECUTION_REGISTRY.register(name, executor)

        _TOOL_EXECUTION_REGISTRY_INITIALIZED = True


def _get_tool_runtime_service() -> ToolRuntimeService:
    global _TOOL_RUNTIME_SERVICE
    if _TOOL_RUNTIME_SERVICE is not None:
        return _TOOL_RUNTIME_SERVICE

    from app.tools.resolver import ToolRuntimeResolver

    async def _fallback_execute(tool_name: str, arguments: dict, context) -> str:
        return await _execute_mcp_tool(tool_name, arguments, agent_id=context.agent_id)

    async def _direct_fallback_execute(tool_name: str, arguments: dict, context) -> str:
        # Direct execution after approval still uses the same first-class registry.
        # If the registry does not recognize the tool, only then do we fall back to
        # MCP passthrough for unknown/remote tools.
        return await _execute_mcp_tool(tool_name, arguments, agent_id=context.agent_id)

    async def _log_activity(*args, **kwargs) -> None:
        from app.services.activity_logger import log_activity
        await log_activity(*args, **kwargs)

    _TOOL_RUNTIME_SERVICE = ToolRuntimeService(
        runtime_resolver=ToolRuntimeResolver(),
        governance_resolver=ToolGovernanceResolver(),
        registry=_TOOL_EXECUTION_REGISTRY,
        ensure_registry=_ensure_tool_execution_registry,
        governance_runner=run_tool_governance,
        fallback_executor=_fallback_execute,
        direct_fallback_executor=_direct_fallback_execute,
        activity_logger=_log_activity,
    )
    return _TOOL_RUNTIME_SERVICE


def _filter_feishu_tools_for_access(
    tools: list[dict],
    *,
    has_feishu_channel: bool,
    has_feishu_office_access: bool,
    has_feishu_cli_access: bool,
) -> list[dict]:
    """Compatibility facade for Feishu tool access filtering."""
    return _surface_filter_feishu_tools_for_access(
        tools,
        has_feishu_channel=has_feishu_channel,
        has_feishu_office_access=has_feishu_office_access,
        has_feishu_cli_access=has_feishu_cli_access,
    )


async def _agent_has_feishu(agent_id: uuid.UUID) -> bool:
    """Compatibility facade for Feishu channel presence checks."""
    return await _surface_agent_has_feishu(agent_id)


async def _agent_has_feishu_office_access(agent_id: uuid.UUID) -> bool:
    """Compatibility facade for Feishu office access checks."""
    return await _surface_agent_has_feishu_office_access(agent_id)


async def _agent_has_feishu_cli_access() -> bool:
    """Compatibility facade for Feishu CLI access checks."""
    return await _surface_agent_has_feishu_cli_access()


# ─── Dynamic Tool Loading from DB ──────────────────────────────

async def get_agent_tools_for_llm(
    agent_id: uuid.UUID,
    core_only: bool = False,
    requested_names: list[str] | None = None,
) -> list[dict]:
    """Compatibility facade for runtime tool surface selection."""
    return await _get_agent_tools_for_llm(
        agent_id,
        core_only=core_only,
        requested_names=requested_names,
    )


# ─── Tool Executors ─────────────────────────────────────────────


async def _execute_tool_direct(
    tool_name: str,
    arguments: dict,
    agent_id: uuid.UUID,
) -> str:
    """Execute a tool directly, bypassing approval preflight checks.

    Used by the approval post-processing hook after an action
    has been approved and needs to actually run.
    """
    return await _get_tool_runtime_service().execute_direct(
        tool_name,
        arguments,
        agent_id=agent_id,
    )


async def execute_tool(
    tool_name: str,
    arguments: dict,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    event_callback: ToolEventCallback | None = None,
) -> str:
    """Execute a tool call and return the result as a string."""
    return await _get_tool_runtime_service().execute(
        tool_name,
        arguments,
        agent_id=agent_id,
        user_id=user_id,
        event_callback=event_callback,
    )


async def _execute_tool_inner(
    tool_name: str,
    arguments: dict,
    context,
) -> str:
    """Inner tool dispatch — called with timeout wrapper from execute_tool()."""
    return await _get_tool_runtime_service().execute_with_context(
        tool_name,
        arguments,
        context,
    )


# ─── Domain module re-exports ──────────────────────────────────
# All business logic lives in agent_tool_domains/. These re-exports
# preserve backward compatibility for existing import sites.
from app.services.agent_tool_domains.workspace import (  # noqa: E402
    _build_skill_registry as _build_skill_registry,
    _delete_file as _delete_file,
    _edit_file as _edit_file,
    _glob_search as _glob_search,
    _grep_search as _grep_search,
    _list_files as _list_files,
    _load_skill as _load_skill,
    _read_document as _read_document,
    _read_file as _read_file,
    _tool_search as _tool_search,
    _write_file as _write_file,
)
from app.services.agent_tool_domains.tasks import (  # noqa: E402
    _manage_tasks as _manage_tasks,
)
from app.services.agent_tool_domains.plaza import (  # noqa: E402
    _plaza_get_new_posts as _plaza_get_new_posts,
    _plaza_create_post as _plaza_create_post,
    _plaza_add_comment as _plaza_add_comment,
)
from app.services.agent_tool_domains.code_exec import (  # noqa: E402
    _check_code_safety as _check_code_safety,
    _execute_code as _execute_code,
    _run_command as _run_command,
)
from app.services.agent_tool_domains.image_upload import (  # noqa: E402
    _upload_image as _upload_image,
)
from app.services.agent_tool_domains.email import (  # noqa: E402
    _get_email_config as _get_email_config,
    _handle_email_tool as _handle_email_tool,
)
from app.services.agent_tool_domains.triggers import (  # noqa: E402
    MAX_TRIGGERS_PER_AGENT as MAX_TRIGGERS_PER_AGENT,
    VALID_TRIGGER_TYPES as VALID_TRIGGER_TYPES,
    _handle_set_trigger as _handle_set_trigger,
    _handle_update_trigger as _handle_update_trigger,
    _handle_cancel_trigger as _handle_cancel_trigger,
    _handle_list_triggers as _handle_list_triggers,
)
from app.services.agent_tool_domains.messaging import (  # noqa: E402
    A2A_SYSTEM_PROMPT_SUFFIX as A2A_SYSTEM_PROMPT_SUFFIX,
    _send_feishu_message as _send_feishu_message,
    _send_web_message as _send_web_message,
    _persist_agent_tool_call as _persist_agent_tool_call,
    _build_agent_message_tool_executor as _build_agent_message_tool_executor,
    _invoke_agent_message_runtime as _invoke_agent_message_runtime,
    _send_message_to_agent as _send_message_to_agent,
    _delegate_to_agent_async as _delegate_to_agent_async,
    _check_async_task as _check_async_task,
    _cancel_async_task as _cancel_async_task,
    _list_async_tasks as _list_async_tasks,
    _get_current_time as _get_current_time,
)
from app.services.agent_tool_domains.feishu_helpers import (  # noqa: E402
    _get_feishu_token as _get_feishu_token,
    _get_agent_calendar_id as _get_agent_calendar_id,
    _feishu_resolve_open_id as _feishu_resolve_open_id,
    _iso_to_ts as _iso_to_ts,
)
from app.services.agent_tool_domains.feishu_wiki import (  # noqa: E402
    _feishu_wiki_get_node as _feishu_wiki_get_node,
    _feishu_wiki_list as _feishu_wiki_list,
)
from app.services.agent_tool_domains.feishu_docs import (  # noqa: E402
    _feishu_doc_read as _feishu_doc_read,
    _feishu_doc_create as _feishu_doc_create,
    _parse_inline_markdown as _parse_inline_markdown,
    _markdown_to_feishu_blocks as _markdown_to_feishu_blocks,
    _feishu_doc_append as _feishu_doc_append,
    _feishu_doc_delete as _feishu_doc_delete,
)
from app.services.agent_tool_domains.feishu_sheets import (  # noqa: E402
    _feishu_sheet_info as _feishu_sheet_info,
    _feishu_sheet_read as _feishu_sheet_read,
)
from app.services.agent_tool_domains.feishu_base import (  # noqa: E402
    _feishu_base_app_create as _feishu_base_app_create,
    _feishu_base_field_list as _feishu_base_field_list,
    _feishu_base_field_create as _feishu_base_field_create,
    _feishu_base_table_list as _feishu_base_table_list,
    _feishu_base_record_delete as _feishu_base_record_delete,
    _feishu_base_record_list as _feishu_base_record_list,
    _feishu_base_record_upload_attachment as _feishu_base_record_upload_attachment,
    _feishu_base_record_upsert as _feishu_base_record_upsert,
)
from app.services.agent_tool_domains.feishu_approval import (  # noqa: E402
    _feishu_approval_create as _feishu_approval_create,
    _feishu_approval_query as _feishu_approval_query,
    _feishu_approval_get as _feishu_approval_get,
)
from app.services.agent_tool_domains.feishu_tasks import (  # noqa: E402
    _feishu_task_comment as _feishu_task_comment,
    _feishu_task_complete as _feishu_task_complete,
    _feishu_task_create as _feishu_task_create,
    _feishu_task_list as _feishu_task_list,
)
from app.services.agent_tool_domains.feishu_sharing import (  # noqa: E402
    _feishu_doc_share as _feishu_doc_share,
)
from app.services.agent_tool_domains.feishu_calendar import (  # noqa: E402
    _feishu_calendar_list as _feishu_calendar_list,
    _feishu_calendar_create as _feishu_calendar_create,
    _feishu_calendar_update as _feishu_calendar_update,
    _feishu_calendar_delete as _feishu_calendar_delete,
)
from app.services.agent_tool_domains.feishu_users import (  # noqa: E402
    _feishu_user_search as _feishu_user_search,
    _feishu_contacts_refresh as _feishu_contacts_refresh,
)


from app.services.agent_tool_domains.web_mcp import (  # noqa: E402
    _discover_resources as _discover_resources,
    _execute_mcp_tool as _execute_mcp_tool,
    _execute_via_smithery_connect as _execute_via_smithery_connect,
    _firecrawl_fetch as _firecrawl_fetch,
    _get_exa_api_key as _get_exa_api_key,
    _get_firecrawl_api_key as _get_firecrawl_api_key,
    _get_xcrawl_api_key as _get_xcrawl_api_key,
    _import_mcp_server as _import_mcp_server,
    _search_exa as _search_exa,
    _search_bing as _search_bing,
    _search_duckduckgo as _search_duckduckgo,
    _search_google as _search_google,
    _search_tavily as _search_tavily,
    _smithery_auto_recover as _smithery_auto_recover,
    _web_fetch as _web_fetch,
    _web_search as _web_search,
    _xcrawl_scrape as _xcrawl_scrape,
)
