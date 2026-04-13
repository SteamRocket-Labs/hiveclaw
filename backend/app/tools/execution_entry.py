"""Canonical runtime execution entry for first-class and MCP-backed tools."""

from __future__ import annotations

import threading
import uuid
from typing import Awaitable, Callable

from app.tools import (
    ToolExecutionRegistry,
    ToolGovernanceResolver,
    ToolRuntimeService,
    run_tool_governance,
)
from app.tools.surface import get_collected_tools

ToolEventCallback = Callable[[dict], Awaitable[None] | None]

_TOOL_EXECUTION_REGISTRY = ToolExecutionRegistry()
_TOOL_EXECUTION_REGISTRY_INITIALIZED = False
_TOOL_RUNTIME_SERVICE: ToolRuntimeService | None = None
_REGISTRY_LOCK = threading.Lock()


def _ensure_tool_execution_registry() -> None:
    global _TOOL_EXECUTION_REGISTRY_INITIALIZED
    if _TOOL_EXECUTION_REGISTRY_INITIALIZED:
        return

    with _REGISTRY_LOCK:
        if _TOOL_EXECUTION_REGISTRY_INITIALIZED:
            return

        collected = get_collected_tools()
        for name, executor in collected.exec_registry._executors.items():
            _TOOL_EXECUTION_REGISTRY.register(name, executor)

        _TOOL_EXECUTION_REGISTRY_INITIALIZED = True


def _get_tool_runtime_service() -> ToolRuntimeService:
    global _TOOL_RUNTIME_SERVICE
    if _TOOL_RUNTIME_SERVICE is not None:
        return _TOOL_RUNTIME_SERVICE

    from app.tools.resolver import ToolRuntimeResolver

    async def _fallback_execute(tool_name: str, arguments: dict, context) -> str:
        from app.services.agent_tool_domains.web_mcp import _execute_mcp_tool

        return await _execute_mcp_tool(tool_name, arguments, agent_id=context.agent_id)

    async def _direct_fallback_execute(tool_name: str, arguments: dict, context) -> str:
        from app.services.agent_tool_domains.web_mcp import _execute_mcp_tool

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


async def execute_tool_direct(
    tool_name: str,
    arguments: dict,
    agent_id: uuid.UUID,
) -> str:
    """Execute a tool directly, bypassing approval preflight checks."""
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


async def execute_tool_inner(
    tool_name: str,
    arguments: dict,
    context,
) -> str:
    """Inner tool dispatch used by timeout wrappers and injected executors."""
    return await _get_tool_runtime_service().execute_with_context(
        tool_name,
        arguments,
        context,
    )
