from __future__ import annotations

import contextlib
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


def test_deferred_tool_risk_is_sourced_from_tool_meta_not_name_tokens():
    from app.services.agent_tools import _deferred_tool_risk_for_name

    assert _deferred_tool_risk_for_name("team_create") == "side_effect_governed"
    assert _deferred_tool_risk_for_name("mcp__notion__search") == "external_mcp"
    assert _deferred_tool_risk_for_name("create_totally_unknown_thing") == "unclassified"


@pytest.mark.asyncio
async def test_deferred_tool_candidates_project_activation_keys(monkeypatch):
    from app.services.agent_tools import available_deferred_tool_candidates_for_agent

    async def fake_available_deferred_tool_names_for_agent(_agent_id, *, limit=80):
        assert limit == 80
        return ["team_create"]

    monkeypatch.setattr(
        "app.services.agent_tools.available_deferred_tool_names_for_agent",
        fake_available_deferred_tool_names_for_agent,
    )

    candidates = await available_deferred_tool_candidates_for_agent(uuid4())

    assert len(candidates) == 1
    keys = candidates[0]["activation_keys"]
    assert keys["schema_version"] == "runtime.metadata_activation_keys.20260705"
    assert keys["candidate_kind"] == "tool"
    assert keys["candidate_ref"]["kind"] == "tool_schema"
    assert keys["key_features"]["name"] == ["team_create"]
    assert keys["key_features"]["group"] == ["agent_team"]
    assert keys["key_features"]["risk"] == ["side_effect_governed"]
    assert keys["value_pointer"]["loader"] == "tool_search"
    assert keys["value_pointer"]["selector"] == "select:team_create"


@pytest.mark.asyncio
async def test_deferred_tool_gatherer_outputs_activation_candidates(monkeypatch):
    from app.services.agent_tools import gather_deferred_tool_candidates_for_agent

    async def fake_available_deferred_tool_names_for_agent(_agent_id, *, limit=80):
        assert limit == 80
        return ["team_create"]

    monkeypatch.setattr(
        "app.services.agent_tools.available_deferred_tool_names_for_agent",
        fake_available_deferred_tool_names_for_agent,
    )

    candidates = await gather_deferred_tool_candidates_for_agent(uuid4())

    assert len(candidates) == 1
    manifest = candidates[0].to_manifest()
    assert manifest["candidate_kind"] == "tool"
    assert manifest["candidate_ref"]["source_type"] == "deferred_tool_catalog"
    assert manifest["key_features"]["name"] == ["team_create"]
    assert manifest["key_features"]["group"] == ["agent_team"]
    assert manifest["value_pointer"]["loader"] == "tool_search"
    assert manifest["value_pointer"]["selector"] == "select:team_create"
    assert manifest["surface"]["surface_kind"] == "deferred_tool_catalog"
    assert manifest["source_refs"] == ["tool:team_create"]


@pytest.mark.asyncio
async def test_execute_approved_tool_forwards_only_ticket_identity(monkeypatch):
    from app.services.agent_tools import execute_approved_tool

    agent_id = uuid4()
    approver_id = uuid4()
    approval_id = uuid4()
    captured = {}

    class Runtime:
        async def execute_approved(self, **kwargs):
            captured.update(kwargs)
            return "ticket-ok"

    monkeypatch.setattr("app.services.agent_tools._get_tool_runtime_service", lambda: Runtime())

    result = await execute_approved_tool(
        approval_id=approval_id,
        expected_agent_id=agent_id,
        approved_by_user_id=approver_id,
    )

    assert result == "ticket-ok"
    assert captured == {
        "approval_id": approval_id,
        "expected_agent_id": agent_id,
        "approved_by_user_id": approver_id,
    }


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
async def test_get_agent_tools_for_llm_keeps_keyless_web_providers_visible(monkeypatch):
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
    assert "advanced_web_search" in names
    assert "advanced_web_fetch" in names
    assert "anysearch_get_sub_domains" in names
    assert "anysearch_search" in names
    assert "anysearch_batch_search" in names
    assert "anysearch_extract" in names
    assert "exa_search" in names
    assert "exa_fetch" in names
    assert "tavily_search" in names
    assert "tavily_extract" in names
    assert "firecrawl_search" in names
    assert "firecrawl_fetch" in names
    assert "xcrawl_scrape" not in names
    assert "discover_resources" not in names
    assert "import_mcp_server" not in names


@pytest.mark.asyncio
async def test_provider_available_tools_respects_explicit_api_key_mode(monkeypatch):
    from app.services import agent_tools as agent_tools_module

    async def no_key() -> str:
        return ""

    async def api_key_required_config(tool_name: str) -> dict:
        if tool_name == "web_search":
            return {"anysearch_auth_mode": "api_key", "anysearch_api_keys": ""}
        if tool_name in {
            "advanced_web_search",
            "advanced_web_fetch",
            "exa_search",
            "exa_fetch",
            "tavily_search",
            "tavily_extract",
            "firecrawl_search",
            "firecrawl_fetch",
        }:
            return {"auth_mode": "api_key", "api_key": ""}
        return {}

    async def no_smithery_key(_agent_id=None) -> str:
        return ""

    async def no_modelscope_token() -> str:
        return ""

    monkeypatch.setattr(agent_tools_module, "_get_exa_api_key", no_key)
    monkeypatch.setattr(agent_tools_module, "_get_tavily_api_key", no_key)
    monkeypatch.setattr(agent_tools_module, "_get_firecrawl_api_key", no_key)
    monkeypatch.setattr(agent_tools_module, "_get_xcrawl_api_key", no_key)
    monkeypatch.setattr(agent_tools_module, "_get_tool_config", api_key_required_config)
    monkeypatch.setattr("app.services.resource_discovery._get_smithery_api_key", no_smithery_key)
    monkeypatch.setattr("app.services.resource_discovery._get_modelscope_api_token", no_modelscope_token)

    available = await agent_tools_module._provider_available_tools()

    assert "advanced_web_search" not in available
    assert "advanced_web_fetch" not in available
    assert "anysearch_get_sub_domains" not in available
    assert "anysearch_search" not in available
    assert "anysearch_batch_search" not in available
    assert "anysearch_extract" not in available
    assert "exa_search" not in available
    assert "exa_fetch" not in available
    assert "tavily_search" not in available
    assert "tavily_extract" not in available
    assert "firecrawl_search" not in available
    assert "firecrawl_fetch" not in available
    assert "xcrawl_scrape" not in available


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
