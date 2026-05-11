from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)


def _make_tool(*, name: str, category: str = "general", is_default: bool = False):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        display_name=name.replace("_", " ").title(),
        description=f"{name} description",
        type="builtin",
        category=category,
        icon="🔧",
        parameters_schema={"type": "object", "properties": {}},
        enabled=True,
        is_default=is_default,
        tenant_id=None,
        mcp_server_name=None,
        mcp_server_url=None,
    )


@pytest.mark.asyncio
async def test_execute_approved_tool_prefers_tool_registry_executor(monkeypatch):
    from app.services.agent_tools import execute_approved_tool
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

    result = await execute_approved_tool(
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
async def test_execute_approved_tool_registry_miss_uses_mcp_passthrough(monkeypatch):
    from app.services.agent_tools import execute_approved_tool
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

    async def fake_execute_mcp(tool_name, arguments, *, agent_id):
        called["tool_name"] = tool_name
        called["arguments"] = arguments
        called["agent_id"] = agent_id
        return "mcp-passthrough"

    monkeypatch.setattr("app.tools.resolver.ToolRuntimeResolver.resolve", fake_resolve)
    monkeypatch.setattr("app.services.agent_tools._ensure_tool_execution_registry", lambda: None)
    monkeypatch.setattr("app.services.agent_tools._TOOL_EXECUTION_REGISTRY.try_execute", fake_try_execute)
    monkeypatch.setattr("app.services.agent_tools._execute_mcp_tool", fake_execute_mcp)
    agent_id = uuid4()

    result = await execute_approved_tool(
        "execute_code",
        {"language": "python", "code": "print('hi')"},
        agent_id,
    )

    assert result == "mcp-passthrough"
    assert called["tool_name"] == "execute_code"
    assert called["arguments"] == {"language": "python", "code": "print('hi')"}
    assert called["agent_id"] == agent_id


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
async def test_get_agent_tools_for_llm_core_only_matches_first_round_surface(monkeypatch):
    from app.services import agent_tools as agent_tools_module

    class BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def broken_async_session():
        return BrokenSession()

    monkeypatch.setattr(agent_tools_module, "async_session", broken_async_session)

    tools = await agent_tools_module.get_agent_tools_for_llm(uuid4(), core_only=True)
    names = {tool["function"]["name"] for tool in tools}

    assert "search_memory" in names
    assert "save_memory" in names
    assert "list_triggers" in names
    assert "update_trigger" in names
    assert "cancel_trigger" in names
    assert "web_search" not in names


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
async def test_get_agent_tools_for_llm_requested_skill_tool_can_expand_non_default_without_assignment(monkeypatch):
    from app.services import agent_tools as agent_tools_module

    agent_id = uuid4()
    tenant_id = uuid4()
    deep_research_tool = _make_tool(
        name="deep_research_run",
        category="deep_research_pack",
        is_default=False,
    )
    default_core_tool = _make_tool(
        name="read_file",
        category="filesystem",
        is_default=True,
    )

    def fake_async_session():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ScalarResult(None),  # no tenant pack policy row
                _ListResult([default_core_tool, deep_research_tool]),
                _ListResult([]),  # no AgentTool assignment exists yet
            ]
        )

    async def no_feishu_channel(_agent_id):
        return False

    async def no_feishu_cli_access():
        return False

    monkeypatch.setattr(agent_tools_module, "async_session", fake_async_session)
    monkeypatch.setattr(agent_tools_module, "_agent_has_feishu", no_feishu_channel)
    monkeypatch.setattr(agent_tools_module, "_agent_has_feishu_cli_access", no_feishu_cli_access)

    tools = await agent_tools_module.get_agent_tools_for_llm(
        agent_id,
        requested_names=["deep_research_run"],
    )
    names = {tool["function"]["name"] for tool in tools}

    assert "deep_research_run" in names


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
