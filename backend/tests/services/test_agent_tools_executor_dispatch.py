from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_execute_tool_inner_prefers_tool_execution_registry(monkeypatch):
    from app.core.execution_context import ExecutionIdentity
    from app.services import agent_tools as agent_tools_module
    from app.tools.runtime import ToolExecutionContext

    agent_id = uuid4()
    user_id = uuid4()
    captured = {}

    async def fake_try_execute(request):
        captured["request"] = request
        return "FROM_REGISTRY"

    monkeypatch.setattr(agent_tools_module._TOOL_EXECUTION_REGISTRY, "try_execute", fake_try_execute)
    monkeypatch.setattr(
        "app.core.execution_context.get_execution_identity",
        lambda: ExecutionIdentity(
            identity_type="delegated_user",
            identity_id=user_id,
            label="Rocky via web",
        ),
    )

    result = await agent_tools_module._execute_tool_inner(
        "list_files",
        {"path": "skills"},
        ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id="tenant-1",
            workspace=Path("/tmp/ws"),
            execution_identity=ExecutionIdentity(
                identity_type="delegated_user",
                identity_id=user_id,
                label="Rocky via web",
            ),
        ),
    )

    assert result == "FROM_REGISTRY"
    assert captured["request"].tool_name == "list_files"
    assert captured["request"].arguments == {"path": "skills"}
    assert captured["request"].context.agent_id == agent_id
    assert captured["request"].context.user_id == user_id
    assert captured["request"].context.tenant_id == "tenant-1"
    assert captured["request"].context.workspace == Path("/tmp/ws")
    assert captured["request"].context.execution_identity.identity_type == "delegated_user"
    assert captured["request"].context.execution_identity.identity_id == user_id


@pytest.mark.asyncio
async def test_execute_tool_inner_falls_back_to_mcp_executor(monkeypatch):
    from app.services import agent_tools as agent_tools_module
    from app.tools.runtime import ToolExecutionContext

    agent_id = uuid4()
    user_id = uuid4()
    captured = {}

    async def fake_try_execute(_request):
        return None

    async def fake_execute_mcp_tool(tool_name, arguments, agent_id=None):
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        captured["agent_id"] = agent_id
        return "FROM_MCP"

    monkeypatch.setattr(agent_tools_module._TOOL_EXECUTION_REGISTRY, "try_execute", fake_try_execute)
    monkeypatch.setattr(agent_tools_module, "_execute_mcp_tool", fake_execute_mcp_tool)

    result = await agent_tools_module._execute_tool_inner(
        "custom_remote_tool",
        {"query": "agent"},
        ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id="tenant-1",
            workspace=Path("/tmp/ws"),
        ),
    )

    assert result == "FROM_MCP"
    assert captured == {
        "tool_name": "custom_remote_tool",
        "arguments": {"query": "agent"},
        "agent_id": agent_id,
    }
