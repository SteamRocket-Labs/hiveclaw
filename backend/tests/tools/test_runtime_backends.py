from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeRuntimeResolver:
    def __init__(self, context):
        self.context = context

    async def resolve(self, *, agent_id, user_id):
        return self.context


class _FakeRegistry:
    async def try_execute(self, request):
        return f"registry:{request.tool_name}"


@pytest.mark.asyncio
async def test_tool_runtime_service_routes_execution_through_backend():
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    class CapturingBackend:
        name = "capturing"

        def __init__(self):
            self.calls = []

        async def execute(self, request, executor):
            self.calls.append(request)
            result = await executor(request)
            return f"{self.name}:{result}"

    agent_id = uuid4()
    user_id = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    backend = CapturingBackend()
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=SimpleNamespace(
            build_context=lambda **_kwargs: SimpleNamespace(),
            build_dependencies=lambda: SimpleNamespace(),
        ),
        registry=_FakeRegistry(),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        backend=backend,
    )

    result = await service.execute_with_context("read_file", {"path": "focus.md"}, context)

    assert result == "capturing:registry:read_file"
    assert backend.calls[0].tool_name == "read_file"
    assert backend.calls[0].context is context


@pytest.mark.asyncio
async def test_disabled_docker_runtime_backend_fails_closed():
    from app.tools.backends import DockerToolRuntimeBackend
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    backend = DockerToolRuntimeBackend(image="python:3.12-slim", enabled=False)
    request = ToolExecutionRequest(
        tool_name="run_command",
        arguments={"cmd": "echo hi"},
        context=ToolExecutionContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id="tenant-1",
            workspace=Path("/tmp/ws"),
        ),
    )

    async def executor(_request):
        raise AssertionError("disabled docker backend must not execute payloads")

    result = await backend.execute(request, executor)

    assert "<tool_error>" in result
    assert "backend_unavailable" in result
    assert "docker" in result
