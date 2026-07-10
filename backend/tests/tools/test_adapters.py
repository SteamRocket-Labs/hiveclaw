"""Tests for the adapter layer bridging ToolExecutionRequest → handler signatures."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.tools.adapters import adapt_and_call
from app.tools.decorator import ToolMeta
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


def _make_request(**overrides) -> ToolExecutionRequest:
    ctx = ToolExecutionContext(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/test-ws"),
    )
    return ToolExecutionRequest(
        tool_name=overrides.get("tool_name", "test"),
        arguments=overrides.get("arguments", {"q": "hello"}),
        context=ctx,
    )


def _make_meta(adapter: str) -> ToolMeta:
    return ToolMeta(
        name="test_tool",
        description="x",
        parameters={},
        category="test",
        display_name="X",
        adapter=adapter,
    )


@pytest.mark.asyncio
async def test_adapter_request():
    captured = {}

    async def handler(request: ToolExecutionRequest) -> str:
        captured["request"] = request
        return "ok"

    req = _make_request()
    result = await adapt_and_call(_make_meta("request"), handler, req)
    assert result == "ok"
    assert captured["request"] is req


@pytest.mark.asyncio
async def test_adapter_args_only():
    captured = {}

    async def handler(arguments: dict) -> str:
        captured["args"] = arguments
        return "searched"

    req = _make_request(arguments={"q": "test"})
    result = await adapt_and_call(_make_meta("args_only"), handler, req)
    assert result == "searched"
    assert captured["args"] == {"q": "test"}


@pytest.mark.asyncio
async def test_adapter_agent_args():
    captured = {}

    async def handler(agent_id: uuid.UUID, arguments: dict) -> str:
        captured["agent_id"] = agent_id
        captured["args"] = arguments
        return "done"

    req = _make_request()
    result = await adapt_and_call(_make_meta("agent_args"), handler, req)
    assert result == "done"
    assert captured["agent_id"] == req.context.agent_id
    assert captured["args"] == req.arguments


@pytest.mark.asyncio
async def test_adapter_agent_args_passes_optional_tenant_id():
    captured = {}

    async def handler(agent_id: uuid.UUID, arguments: dict, tenant_id: str | None) -> str:
        captured["agent_id"] = agent_id
        captured["args"] = arguments
        captured["tenant_id"] = tenant_id
        return "done"

    req = _make_request()
    result = await adapt_and_call(_make_meta("agent_args"), handler, req)
    assert result == "done"
    assert captured["agent_id"] == req.context.agent_id
    assert captured["args"] == req.arguments
    assert captured["tenant_id"] == req.context.tenant_id


@pytest.mark.asyncio
async def test_adapter_agent_only():
    captured = {}

    async def handler(agent_id: uuid.UUID) -> str:
        captured["agent_id"] = agent_id
        return "listed"

    req = _make_request()
    result = await adapt_and_call(_make_meta("agent_only"), handler, req)
    assert result == "listed"
    assert captured["agent_id"] == req.context.agent_id


@pytest.mark.asyncio
async def test_adapter_agent_workspace_args():
    captured = {}

    async def handler(agent_id: uuid.UUID, workspace: Path, arguments: dict) -> str:
        captured.update(agent_id=agent_id, workspace=workspace, args=arguments)
        return "sent"

    req = _make_request()
    result = await adapt_and_call(_make_meta("agent_workspace_args"), handler, req)
    assert result == "sent"
    assert captured["workspace"] == req.context.workspace


@pytest.mark.asyncio
async def test_adapter_workspace_args():
    captured = {}

    async def handler(workspace: Path, arguments: dict, tenant_id: str | None) -> str:
        captured.update(workspace=workspace, args=arguments, tenant_id=tenant_id)
        return "read"

    req = _make_request()
    result = await adapt_and_call(_make_meta("workspace_args"), handler, req)
    assert result == "read"
    assert captured["workspace"] == req.context.workspace
    assert captured["tenant_id"] == "tenant-1"


@pytest.mark.asyncio
async def test_adapter_sync_handler():
    """Sync handlers should also work."""

    def handler(arguments: dict) -> str:
        return f"sync-{arguments['q']}"

    req = _make_request(arguments={"q": "test"})
    result = await adapt_and_call(_make_meta("args_only"), handler, req)
    assert result == "sync-test"


@pytest.mark.asyncio
async def test_adapter_unknown_raises():
    async def handler(x: str) -> str:
        return x

    req = _make_request()
    with pytest.raises(ValueError, match="Unknown adapter type"):
        await adapt_and_call(_make_meta("nonexistent"), handler, req)
