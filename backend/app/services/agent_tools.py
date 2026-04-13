"""Compatibility facade for tool surface and runtime entry."""

import uuid

from app.tools.execution_entry import (
    execute_tool as _execute_tool_entry,
    execute_tool_direct as _execute_tool_direct_entry,
    execute_tool_inner as _execute_tool_inner_entry,
)
from app.tools.surface import (
    CORE_TOOL_NAMES as _CORE_TOOL_NAMES,
    get_agent_tools_for_llm as _get_agent_tools_for_llm,
    get_collected_tools as _surface_get_collected_tools,
    get_combined_openai_tools as _get_combined_openai_tools,
)

CORE_TOOL_NAMES = _CORE_TOOL_NAMES


def _get_collected_tools():
    """Compatibility facade for collected tool metadata/executors."""
    return _surface_get_collected_tools()


def get_combined_openai_tools() -> list[dict]:
    """Compatibility facade for the canonical OpenAI tool surface."""
    return _get_combined_openai_tools()


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
    return await _execute_tool_direct_entry(
        tool_name,
        arguments,
        agent_id=agent_id,
    )


async def execute_tool(
    tool_name: str,
    arguments: dict,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    event_callback=None,
) -> str:
    """Execute a tool call and return the result as a string."""
    return await _execute_tool_entry(
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
    return await _execute_tool_inner_entry(
        tool_name,
        arguments,
        context,
    )
