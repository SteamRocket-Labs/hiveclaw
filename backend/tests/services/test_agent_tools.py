from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_execute_tool_direct_prefers_tool_registry_executor(monkeypatch):
    from app.services.agent_tools import _execute_tool_direct
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    workspace = Path("/tmp/test-agent-workspace")
    agent_id = uuid4()
    captured = {}

    async def fake_resolve(self, *, agent_id: object, user_id: object):
        return ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id="tenant-1",
            workspace=workspace,
        )

    async def fake_try_execute(request: ToolExecutionRequest):
        captured["request"] = request
        return "registry-ok"

    monkeypatch.setattr("app.tools.resolver.ToolRuntimeResolver.resolve", fake_resolve)
    monkeypatch.setattr("app.services.agent_tools._ensure_tool_execution_registry", lambda: None)
    monkeypatch.setattr("app.services.agent_tools._TOOL_EXECUTION_REGISTRY.try_execute", fake_try_execute)

    result = await _execute_tool_direct(
        "execute_code",
        {"language": "python", "code": "print('hi')"},
        agent_id,
    )

    assert result == "registry-ok"
    assert captured["request"].tool_name == "execute_code"
    assert captured["request"].arguments == {"language": "python", "code": "print('hi')"}
    assert captured["request"].context.agent_id == agent_id
    assert captured["request"].context.workspace == workspace


@pytest.mark.asyncio
async def test_execute_tool_direct_falls_back_to_execute_code(monkeypatch):
    from app.services.agent_tools import _execute_tool_direct
    from app.tools.runtime import ToolExecutionContext

    workspace = Path("/tmp/test-agent-workspace")
    called = {}

    async def fake_resolve(self, *, agent_id: object, user_id: object):
        return ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id="tenant-1",
            workspace=workspace,
        )

    async def fake_try_execute(_request):
        return None

    async def fake_execute_code(ws, arguments):
        called["ws"] = ws
        called["arguments"] = arguments
        return "ok"

    monkeypatch.setattr("app.tools.resolver.ToolRuntimeResolver.resolve", fake_resolve)
    monkeypatch.setattr("app.services.agent_tools._ensure_tool_execution_registry", lambda: None)
    monkeypatch.setattr("app.services.agent_tools._TOOL_EXECUTION_REGISTRY.try_execute", fake_try_execute)
    monkeypatch.setattr("app.services.agent_tools._execute_code", fake_execute_code)

    result = await _execute_tool_direct(
        "execute_code",
        {"language": "python", "code": "print('hi')"},
        uuid4(),
    )

    assert result == "ok"
    assert called["ws"] == workspace
    assert called["arguments"] == {"language": "python", "code": "print('hi')"}


@pytest.mark.asyncio
async def test_execute_tool_direct_falls_back_to_run_command(monkeypatch):
    from app.services.agent_tools import _execute_tool_direct
    from app.tools.runtime import ToolExecutionContext

    workspace = Path("/tmp/test-agent-workspace")
    called = {}

    async def fake_resolve(self, *, agent_id: object, user_id: object):
        return ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id="tenant-1",
            workspace=workspace,
        )

    async def fake_try_execute(_request):
        return None

    async def fake_run_command(ws, arguments):
        called["ws"] = ws
        called["arguments"] = arguments
        return "ok-command"

    monkeypatch.setattr("app.tools.resolver.ToolRuntimeResolver.resolve", fake_resolve)
    monkeypatch.setattr("app.services.agent_tools._ensure_tool_execution_registry", lambda: None)
    monkeypatch.setattr("app.services.agent_tools._TOOL_EXECUTION_REGISTRY.try_execute", fake_try_execute)
    monkeypatch.setattr("app.services.agent_tools._run_command", fake_run_command)

    result = await _execute_tool_direct(
        "run_command",
        {"command": "pwd"},
        uuid4(),
    )

    assert result == "ok-command"
    assert called["ws"] == workspace
    assert called["arguments"] == {"command": "pwd"}


@pytest.mark.asyncio
async def test_get_agent_tools_for_llm_db_failure_falls_back_to_combined_tools(monkeypatch):
    from app.services import agent_tools as agent_tools_module

    class BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def broken_async_session():
        return BrokenSession()

    monkeypatch.setattr(agent_tools_module, "async_session", broken_async_session)
    monkeypatch.setattr(agent_tools_module, "_always_core_tools", None)
    monkeypatch.setattr(agent_tools_module, "_feishu_tools", None)

    tools = await agent_tools_module.get_agent_tools_for_llm(uuid4())
    names = {tool["function"]["name"] for tool in tools}

    assert "read_file" in names
    assert "load_skill" in names
    assert "web_search" in names


@pytest.mark.asyncio
async def test_get_agent_tools_for_llm_db_failure_still_filters_feishu_access(monkeypatch):
    from app.services import agent_tools as agent_tools_module

    class BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def broken_async_session():
        return BrokenSession()

    async def no_feishu_channel(_agent_id):
        return False

    async def no_feishu_cli_access():
        return False

    monkeypatch.setattr(agent_tools_module, "async_session", broken_async_session)
    monkeypatch.setattr(agent_tools_module, "_agent_has_feishu", no_feishu_channel)
    monkeypatch.setattr(agent_tools_module, "_agent_has_feishu_cli_access", no_feishu_cli_access)

    tools = await agent_tools_module.get_agent_tools_for_llm(uuid4())
    names = {tool["function"]["name"] for tool in tools}

    assert "send_feishu_message" not in names
    assert "feishu_doc_read" not in names
    assert "feishu_base_table_list" not in names


@pytest.mark.asyncio
async def test_get_agent_tools_for_llm_hides_unavailable_external_providers(monkeypatch):
    from app.services import agent_tools as agent_tools_module

    class BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def broken_async_session():
        return BrokenSession()

    async def no_exa_key() -> str:
        return ""

    async def no_firecrawl_key() -> str:
        return ""

    async def no_xcrawl_key() -> str:
        return ""

    async def no_smithery_key(_agent_id=None) -> str:
        return ""

    async def no_modelscope_token() -> str:
        return ""

    monkeypatch.setattr(agent_tools_module, "async_session", broken_async_session)
    monkeypatch.setattr(agent_tools_module, "_get_exa_api_key", no_exa_key)
    monkeypatch.setattr(agent_tools_module, "_get_firecrawl_api_key", no_firecrawl_key)
    monkeypatch.setattr(agent_tools_module, "_get_xcrawl_api_key", no_xcrawl_key)
    monkeypatch.setattr("app.services.resource_discovery._get_smithery_api_key", no_smithery_key)
    monkeypatch.setattr("app.services.resource_discovery._get_modelscope_api_token", no_modelscope_token)

    tools = await agent_tools_module.get_agent_tools_for_llm(uuid4())
    names = {tool["function"]["name"] for tool in tools}

    assert "web_fetch" in names
    assert "firecrawl_fetch" not in names
    assert "xcrawl_scrape" not in names
    assert "discover_resources" not in names
    assert "import_mcp_server" not in names


def test_filter_feishu_tools_for_access_allows_cli_backed_office_tools_without_channel():
    from app.services.agent_tools import _filter_feishu_tools_for_access

    tools = [
        {"function": {"name": "send_feishu_message"}},
        {"function": {"name": "feishu_doc_read"}},
        {"function": {"name": "feishu_sheet_info"}},
    ]

    filtered = _filter_feishu_tools_for_access(
        tools,
        has_feishu_channel=False,
        has_feishu_office_access=True,
        has_feishu_cli_access=True,
    )

    names = {tool["function"]["name"] for tool in filtered}
    assert "send_feishu_message" not in names
    assert "feishu_doc_read" in names
    assert "feishu_sheet_info" in names
