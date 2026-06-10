"""Part 5 — MCP tool gating via the new assignment tables (§8.5).

Validates that ``get_agent_tools_for_llm`` gates ``type="mcp"`` tools through
``_resolve_agent_mcp_gating`` (assignment + per-tool deny overrides) WHEN the
agent has new-table data, and falls back to the legacy ``AgentTool.enabled`` /
``is_default`` logic when it does not — the tool-availability parity safety
fallback. Hive mock-session style: a queued spy modeled on
test_mcp_backfill_service.py + the ``_FakeSession`` context-manager pattern from
test_agent_tools.py. No live DB.
"""

from __future__ import annotations

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


def _make_mcp_tool(*, name: str, tool_id, is_default: bool = False):
    return SimpleNamespace(
        id=tool_id,
        name=name,
        display_name=name.replace("_", " ").title(),
        description=f"{name} description",
        type="mcp",
        category="mcp",
        icon="🔌",
        parameters_schema={"type": "object", "properties": {}},
        enabled=True,
        is_default=is_default,
        tenant_id=None,
        mcp_server_name="GitHub",
        mcp_server_url="https://gh",
    )


def _make_agent_tool(*, tool_id, enabled: bool):
    return SimpleNamespace(id=uuid4(), tool_id=tool_id, enabled=enabled, source="system", config={})


def _make_assignment(*, agent_id, server_id, enabled: bool, default_tool_mode: str = "auto", always_load: bool = False):
    return SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        mcp_server_id=server_id,
        enabled=enabled,
        default_tool_mode=default_tool_mode,
        always_load=always_load,
    )


def _make_server_tool(*, server_id, tool_id, tool_name):
    return SimpleNamespace(id=uuid4(), mcp_server_id=server_id, tool_id=tool_id, mcp_tool_name=tool_name)


def _make_override(*, agent_id, server_id, tool_name, mode):
    return SimpleNamespace(id=uuid4(), agent_id=agent_id, mcp_server_id=server_id, tool_name=tool_name, mode=mode)


def _patch_environment(monkeypatch, module, session_factory):
    """Neutralize feishu/pack/availability side-effects so the test isolates gating."""

    async def no_feishu_channel(_agent_id):
        return False

    async def no_feishu_cli_access():
        return False

    async def passthrough_available_tools(_agent_id, tools):
        return tools

    monkeypatch.setattr(module, "async_session", session_factory)

    # RLS 阶段1: get_agent_tools_for_llm now resolves the agent's tenant and
    # opens a tenant-scoped session. Route tenant_scoped_session through the same
    # fake factory (no real SET LOCAL, so the queued result sequence is intact)
    # and stub the tenant resolver. agent_tools imports both at module level.
    async def _resolve(*_a, **_k):
        return uuid4()

    monkeypatch.setattr(module, "tenant_scoped_session", lambda *_a, **_k: session_factory(), raising=False)
    monkeypatch.setattr(module, "resolve_tenant_for_agent", _resolve, raising=False)
    monkeypatch.setattr(module, "_agent_has_feishu", no_feishu_channel)
    monkeypatch.setattr(module, "_agent_has_feishu_cli_access", no_feishu_cli_access)
    monkeypatch.setattr(module, "_filter_unavailable_tools", passthrough_available_tools)
    # get_tenant_pack_policies is left real (consumes the queued _ScalarResult(None),
    # mirroring test_agent_tools.py). MCP server packs aren't in the manifest, so force
    # the orthogonal pack gate open to isolate the new assignment-table gating.
    monkeypatch.setattr(module, "is_pack_enabled", lambda _policies, _pack: True)


@pytest.mark.asyncio
async def test_gating_parity_with_backfilled_data_mixed_enabled_and_deny(monkeypatch):
    """Agent WITH new-table data reaches exactly what assignment + overrides allow."""
    from app.services import agent_tools as module

    agent_id = uuid4()
    tenant_id = uuid4()
    server_a = uuid4()  # enabled server, one tool denied
    server_b = uuid4()  # disabled server, all its tools excluded

    t_search = _make_mcp_tool(name="issue_search", tool_id=uuid4())
    t_repo = _make_mcp_tool(name="repo_read", tool_id=uuid4())  # denied via override
    t_deploy = _make_mcp_tool(name="deploy_run", tool_id=uuid4())  # on disabled server

    def session_factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ScalarResult(None),  # no tenant pack policy row (also short-circuited by patch)
                _ListResult([t_search, t_repo, t_deploy]),
                _ListResult([]),  # no legacy AgentTool rows — gating must come from new tables
                _ListResult(
                    [
                        _make_assignment(agent_id=agent_id, server_id=server_a, enabled=True),
                        _make_assignment(agent_id=agent_id, server_id=server_b, enabled=False),
                    ]
                ),
                _ListResult(
                    [
                        _make_server_tool(server_id=server_a, tool_id=t_search.id, tool_name="issue_search"),
                        _make_server_tool(server_id=server_a, tool_id=t_repo.id, tool_name="repo_read"),
                        _make_server_tool(server_id=server_b, tool_id=t_deploy.id, tool_name="deploy_run"),
                    ]
                ),
                _ListResult(
                    [_make_override(agent_id=agent_id, server_id=server_a, tool_name="repo_read", mode="deny")]
                ),
            ]
        )

    _patch_environment(monkeypatch, module, session_factory)

    tools = await module.get_agent_tools_for_llm(agent_id)
    names = {tool["function"]["name"] for tool in tools}

    assert "issue_search" in names  # enabled server, not denied
    assert "repo_read" not in names  # deny override
    assert "deploy_run" not in names  # disabled assignment


@pytest.mark.asyncio
async def test_fallback_when_no_new_table_data_keeps_legacy_agent_tool_enabled(monkeypatch):
    """Agent with NO new-table rows falls back to legacy AgentTool.enabled logic."""
    from app.services import agent_tools as module

    agent_id = uuid4()
    tenant_id = uuid4()
    t_mcp = _make_mcp_tool(name="issue_search", tool_id=uuid4(), is_default=False)

    def session_factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ScalarResult(None),
                _ListResult([t_mcp]),
                # Legacy enabled assignment keeps the MCP tool reachable under fallback.
                _ListResult([_make_agent_tool(tool_id=t_mcp.id, enabled=True)]),
                _ListResult([]),  # NO AgentMCPServerAssignment rows → _resolve_agent_mcp_gating returns None
            ]
        )

    _patch_environment(monkeypatch, module, session_factory)

    tools = await module.get_agent_tools_for_llm(agent_id)
    names = {tool["function"]["name"] for tool in tools}

    assert "issue_search" in names


@pytest.mark.asyncio
async def test_disabled_assignment_excludes_that_servers_mcp_tools(monkeypatch):
    """A disabled assignment removes that server's MCP tools even if legacy AgentTool says enabled."""
    from app.services import agent_tools as module

    agent_id = uuid4()
    tenant_id = uuid4()
    server_a = uuid4()
    t_mcp = _make_mcp_tool(name="issue_search", tool_id=uuid4())

    def session_factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ScalarResult(None),
                _ListResult([t_mcp]),
                # Legacy row says enabled — new-table disabled assignment must win.
                _ListResult([_make_agent_tool(tool_id=t_mcp.id, enabled=True)]),
                _ListResult([_make_assignment(agent_id=agent_id, server_id=server_a, enabled=False)]),
                _ListResult([_make_server_tool(server_id=server_a, tool_id=t_mcp.id, tool_name="issue_search")]),
                _ListResult([]),  # no overrides
            ]
        )

    _patch_environment(monkeypatch, module, session_factory)

    tools = await module.get_agent_tools_for_llm(agent_id)
    names = {tool["function"]["name"] for tool in tools}

    assert "issue_search" not in names


@pytest.mark.asyncio
async def test_default_tool_mode_deny_excludes_that_servers_mcp_tools(monkeypatch):
    """A server assignment with default_tool_mode=deny removes all server tools."""
    from app.services import agent_tools as module

    agent_id = uuid4()
    tenant_id = uuid4()
    server_a = uuid4()
    t_mcp = _make_mcp_tool(name="issue_search", tool_id=uuid4())

    def session_factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ScalarResult(None),
                _ListResult([t_mcp]),
                _ListResult([_make_agent_tool(tool_id=t_mcp.id, enabled=True)]),
                _ListResult(
                    [_make_assignment(agent_id=agent_id, server_id=server_a, enabled=True, default_tool_mode="deny")]
                ),
                _ListResult([_make_server_tool(server_id=server_a, tool_id=t_mcp.id, tool_name="issue_search")]),
                _ListResult([]),
            ]
        )

    _patch_environment(monkeypatch, module, session_factory)

    tools = await module.get_agent_tools_for_llm(agent_id)
    names = {tool["function"]["name"] for tool in tools}

    assert "issue_search" not in names


@pytest.mark.asyncio
async def test_always_load_mcp_assignment_keeps_tool_in_core_only_surface(monkeypatch):
    """Server-level always_load makes reachable MCP tools resident in the first tool surface."""
    from app.services import agent_tools as module

    agent_id = uuid4()
    tenant_id = uuid4()
    server_a = uuid4()
    t_mcp = _make_mcp_tool(name="issue_search", tool_id=uuid4())

    def session_factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ScalarResult(None),
                _ListResult([t_mcp]),
                _ListResult([]),
                _ListResult([_make_assignment(agent_id=agent_id, server_id=server_a, enabled=True, always_load=True)]),
                _ListResult([_make_server_tool(server_id=server_a, tool_id=t_mcp.id, tool_name="issue_search")]),
                _ListResult([]),
            ]
        )

    _patch_environment(monkeypatch, module, session_factory)

    tools = await module.get_agent_tools_for_llm(agent_id, core_only=True)
    names = {tool["function"]["name"] for tool in tools}

    assert "issue_search" in names


@pytest.mark.asyncio
async def test_deny_override_excludes_only_that_tool(monkeypatch):
    """A deny override removes exactly one tool; siblings on the same enabled server stay."""
    from app.services import agent_tools as module

    agent_id = uuid4()
    tenant_id = uuid4()
    server_a = uuid4()
    t_keep = _make_mcp_tool(name="issue_search", tool_id=uuid4())
    t_deny = _make_mcp_tool(name="repo_read", tool_id=uuid4())

    def session_factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ScalarResult(None),
                _ListResult([t_keep, t_deny]),
                _ListResult([]),
                _ListResult([_make_assignment(agent_id=agent_id, server_id=server_a, enabled=True)]),
                _ListResult(
                    [
                        _make_server_tool(server_id=server_a, tool_id=t_keep.id, tool_name="issue_search"),
                        _make_server_tool(server_id=server_a, tool_id=t_deny.id, tool_name="repo_read"),
                    ]
                ),
                _ListResult(
                    [_make_override(agent_id=agent_id, server_id=server_a, tool_name="repo_read", mode="deny")]
                ),
            ]
        )

    _patch_environment(monkeypatch, module, session_factory)

    tools = await module.get_agent_tools_for_llm(agent_id)
    names = {tool["function"]["name"] for tool in tools}

    assert "issue_search" in names
    assert "repo_read" not in names
