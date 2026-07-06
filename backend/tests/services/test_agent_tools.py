from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _patch_fake_tenant_session(monkeypatch, session_factory, *, tenant_id=None):
    """RLS 阶段2b/stage-1: ``get_agent_tools_for_llm`` (and ``_agent_has_feishu``)
    resolve the agent's tenant and open a ``tenant_scoped_session``. Route the
    scoped session to the test's fake session factory and stub tenant
    resolution so no real DB / bypass read happens."""
    resolved_tenant = tenant_id or uuid4()

    @contextlib.asynccontextmanager
    async def _fake_tenant_scoped_session(*_a, **_k):
        yield session_factory()

    async def _fake_resolve_tenant_for_agent(*_a, **_k):
        return resolved_tenant

    monkeypatch.setattr("app.services.agent_tools.tenant_scoped_session", _fake_tenant_scoped_session)
    monkeypatch.setattr("app.services.agent_tools.resolve_tenant_for_agent", _fake_resolve_tenant_for_agent)
    return resolved_tenant


def _patch_broken_tenant_session(monkeypatch):
    """Simulate a DB failure deterministically: the tenant-scoped session raises
    on entry (instead of relying on a real PG being unreachable)."""

    @contextlib.asynccontextmanager
    async def _broken_tenant_scoped_session(*_a, **_k):
        raise RuntimeError("db down")
        yield  # pragma: no cover

    async def _fake_resolve_tenant_for_agent(*_a, **_k):
        return uuid4()

    monkeypatch.setattr("app.services.agent_tools.tenant_scoped_session", _broken_tenant_scoped_session)
    monkeypatch.setattr("app.services.agent_tools.resolve_tenant_for_agent", _fake_resolve_tenant_for_agent)


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


def _make_agent_tool_assignment(*, tool_id, enabled: bool = True):
    return SimpleNamespace(
        id=uuid4(),
        tool_id=tool_id,
        enabled=enabled,
        source="system",
        config={},
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

    _patch_broken_tenant_session(monkeypatch)
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

    _patch_broken_tenant_session(monkeypatch)

    tools = await agent_tools_module.get_agent_tools_for_llm(uuid4(), core_only=True)
    names = {tool["function"]["name"] for tool in tools}

    assert "search_memory" in names
    assert "load_memory" in names
    assert "save_memory" in names
    assert "update_memory" in names
    assert "retire_memory" in names
    assert "list_triggers" in names
    assert "update_trigger" in names
    assert "cancel_trigger" in names
    assert "web_search" in names  # Step 5: web_search promoted to CORE (turn-1)


@pytest.mark.asyncio
async def test_get_agent_tools_for_llm_db_failure_still_filters_feishu_access(monkeypatch):
    from app.services import agent_tools as agent_tools_module

    async def no_feishu_channel(_agent_id):
        return False

    async def no_feishu_cli_access():
        return False

    _patch_broken_tenant_session(monkeypatch)
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
    office_tool = _make_tool(
        name="office_document_create",
        category="office_pack",
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
                _ListResult([default_core_tool, office_tool]),
                _ListResult([]),  # no AgentTool assignment exists yet
            ]
        )

    async def no_feishu_channel(_agent_id):
        return False

    async def no_feishu_cli_access():
        return False

    _patch_fake_tenant_session(monkeypatch, fake_async_session, tenant_id=tenant_id)
    monkeypatch.setattr(agent_tools_module, "_agent_has_feishu", no_feishu_channel)
    monkeypatch.setattr(agent_tools_module, "_agent_has_feishu_cli_access", no_feishu_cli_access)

    tools = await agent_tools_module.get_agent_tools_for_llm(
        agent_id,
        requested_names=["office_document_create"],
    )
    names = {tool["function"]["name"] for tool in tools}

    assert "office_document_create" in names


@pytest.mark.asyncio
async def test_requested_discovered_tool_does_not_bypass_disabled_pack_policy(monkeypatch):
    from app.services import agent_tools as agent_tools_module

    agent_id = uuid4()
    tenant_id = uuid4()
    exa_tool = _make_tool(
        name="exa_search",
        category="search",
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
                _ListResult([default_core_tool, exa_tool]),
                _ListResult([]),
            ]
        )

    async def no_feishu_channel(_agent_id):
        return False

    async def no_feishu_cli_access():
        return False

    async def disabled_web_capability_group(*_args, **_kwargs):
        return {"web_pack": False}

    async def no_mcp_gating(*_args, **_kwargs):
        return None

    _patch_fake_tenant_session(monkeypatch, fake_async_session, tenant_id=tenant_id)
    monkeypatch.setattr(agent_tools_module, "_agent_has_feishu", no_feishu_channel)
    monkeypatch.setattr(agent_tools_module, "_agent_has_feishu_cli_access", no_feishu_cli_access)
    monkeypatch.setattr(agent_tools_module, "get_agent_capability_group_policies", disabled_web_capability_group)
    monkeypatch.setattr(agent_tools_module, "_resolve_agent_mcp_gating", no_mcp_gating)

    tools = await agent_tools_module.get_agent_tools_for_llm(
        agent_id,
        requested_names=["exa_search"],
    )
    names = {tool["function"]["name"] for tool in tools}

    assert "read_file" in names
    assert "exa_search" not in names


@pytest.mark.asyncio
async def test_get_agent_tools_for_llm_hides_hr_only_assignment_from_regular_agent(monkeypatch):
    from app.services import agent_tools as agent_tools_module

    agent_id = uuid4()
    tenant_id = uuid4()
    hr_tool = _make_tool(
        name="create_digital_employee",
        category="hr",
        is_default=True,
    )
    default_core_tool = _make_tool(
        name="read_file",
        category="filesystem",
        is_default=True,
    )

    def fake_async_session():
        return _FakeSession(
            [
                _ScalarResult(
                    SimpleNamespace(
                        id=agent_id,
                        tenant_id=tenant_id,
                        agent_class="internal_tenant",
                        name="Web3 Researcher",
                    )
                ),
                _ScalarResult(None),  # no tenant pack policy row
                _ListResult([default_core_tool, hr_tool]),
                _ListResult([_make_agent_tool_assignment(tool_id=hr_tool.id, enabled=True)]),
            ]
        )

    async def no_feishu_channel(_agent_id):
        return False

    async def no_feishu_cli_access():
        return False

    async def passthrough_available_tools(_agent_id, tools):
        return tools

    _patch_fake_tenant_session(monkeypatch, fake_async_session, tenant_id=tenant_id)
    monkeypatch.setattr(agent_tools_module, "_agent_has_feishu", no_feishu_channel)
    monkeypatch.setattr(agent_tools_module, "_agent_has_feishu_cli_access", no_feishu_cli_access)
    monkeypatch.setattr(agent_tools_module, "_filter_unavailable_tools", passthrough_available_tools)

    tools = await agent_tools_module.get_agent_tools_for_llm(agent_id)
    names = {tool["function"]["name"] for tool in tools}

    assert "read_file" in names
    assert "create_digital_employee" not in names


@pytest.mark.asyncio
async def test_get_agent_tools_for_llm_hides_unavailable_external_providers(monkeypatch):
    from app.services import agent_tools as agent_tools_module

    async def no_exa_key() -> str:
        return ""

    async def no_tavily_key() -> str:
        return ""

    async def no_firecrawl_key() -> str:
        return ""

    async def no_xcrawl_key() -> str:
        return ""

    async def no_smithery_key(_agent_id=None) -> str:
        return ""

    async def no_modelscope_token() -> str:
        return ""

    _patch_broken_tenant_session(monkeypatch)
    monkeypatch.setattr(agent_tools_module, "_get_exa_api_key", no_exa_key)
    monkeypatch.setattr(agent_tools_module, "_get_tavily_api_key", no_tavily_key)
    monkeypatch.setattr(agent_tools_module, "_get_firecrawl_api_key", no_firecrawl_key)
    monkeypatch.setattr(agent_tools_module, "_get_xcrawl_api_key", no_xcrawl_key)
    monkeypatch.setattr("app.services.resource_discovery._get_smithery_api_key", no_smithery_key)
    monkeypatch.setattr("app.services.resource_discovery._get_modelscope_api_token", no_modelscope_token)

    tools = await agent_tools_module.get_agent_tools_for_llm(uuid4())
    names = {tool["function"]["name"] for tool in tools}

    assert "web_fetch" in names
    assert "web_search" in names
    assert "exa_search" not in names
    assert "tavily_search" not in names
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
