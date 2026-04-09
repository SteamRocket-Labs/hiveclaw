"""Tool execution runtime primitives."""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.core.execution_context import ExecutionIdentity


ToolExecutor = Callable[["ToolExecutionRequest"], Awaitable[str] | str]
FALLBACK_EXECUTOR_NAME = "__mcp_fallback__"


@dataclass(slots=True)
class ToolExecutionContext:
    agent_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: str | None
    workspace: Path
    execution_identity: ExecutionIdentity | None = None


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

    async def try_execute(self, request: ToolExecutionRequest) -> str | None:
        executor = self._executors.get(request.tool_name)
        if executor is None:
            executor = self._executors.get(FALLBACK_EXECUTOR_NAME)
        if executor is None:
            return None
        result = executor(request)
        if inspect.isawaitable(result):
            return await result
        return result
