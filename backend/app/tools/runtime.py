"""Tool execution runtime primitives."""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.core.execution_context import ExecutionIdentity
from app.tools.result_envelope import ToolContentEnvelope

ToolExecutor = Callable[["ToolExecutionRequest"], Awaitable[str | ToolContentEnvelope] | str | ToolContentEnvelope]


@dataclass(slots=True)
class ToolExecutionContext:
    agent_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: str | None
    workspace: Path
    execution_identity: ExecutionIdentity | None = None
    session_id: str | None = None


@dataclass(slots=True)
class ToolExecutionRequest:
    tool_name: str
    arguments: dict[str, Any]
    context: ToolExecutionContext


class ToolExecutionRegistry:
    """Registry for first-class tool executors."""

    def __init__(self) -> None:
        self._executors: dict[str, ToolExecutor] = {}

    def register(self, tool_name: str, executor: ToolExecutor) -> None:
        self._executors[tool_name] = executor

    def names(self) -> frozenset[str]:
        """Return every executable tool name, including aliases."""
        return frozenset(self._executors.keys())

    async def try_execute(self, request: ToolExecutionRequest) -> str | ToolContentEnvelope | None:
        # Pure first-class lookup. An unregistered tool returns None so the single
        # fallback path (ToolRuntimeService.fallback_executor → MCP passthrough)
        # handles it — there is no second in-registry fallback (Step 6 unified the
        # dual execution path; the old __mcp_fallback__ slot was never registered).
        executor = self._executors.get(request.tool_name)
        if executor is None:
            return None

        # Set tenant context for tool config isolation (read by resolve_tool_config)
        from app.core.execution_context import set_tool_tenant_id

        _tenant_id = getattr(request.context, "tenant_id", None) if request.context else None
        set_tool_tenant_id(uuid.UUID(_tenant_id) if isinstance(_tenant_id, str) else _tenant_id)
        try:
            result = executor(request)
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            set_tool_tenant_id(None)
