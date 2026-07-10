"""Execution backend contracts for ToolRuntimeService.

The default backend is local in-process execution. Non-local backends must be
explicit and fail closed until a real sandbox runner is configured.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from app.tools.result_envelope import ToolContentEnvelope, render_tool_error
from app.tools.runtime import ToolExecutionRequest


ToolBackendExecutor = Callable[[ToolExecutionRequest], Awaitable[str | ToolContentEnvelope] | str | ToolContentEnvelope]


class ToolRuntimeBackend(Protocol):
    name: str

    async def execute(self, request: ToolExecutionRequest, executor: ToolBackendExecutor) -> str | ToolContentEnvelope:
        """Run one governed tool request through this backend."""
        ...


async def _maybe_await_result(value):
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(slots=True)
class LocalToolRuntimeBackend:
    name: str = "local"

    async def execute(self, request: ToolExecutionRequest, executor: ToolBackendExecutor) -> str | ToolContentEnvelope:
        return await _maybe_await_result(executor(request))


@dataclass(slots=True)
class DockerToolRuntimeBackend:
    image: str
    enabled: bool = False
    name: str = "docker"

    async def execute(self, request: ToolExecutionRequest, executor: ToolBackendExecutor) -> str | ToolContentEnvelope:
        if not self.enabled:
            return render_tool_error(
                tool_name=request.tool_name,
                error_class="backend_unavailable",
                message=(
                    f"Tool runtime backend '{self.name}' is configured with image '{self.image}' but is not enabled."
                ),
                provider="tool_runtime_backend",
                retryable=False,
                actionable_hint="Use the local backend or enable a configured Docker sandbox runner.",
            )
        return await _maybe_await_result(executor(request))
