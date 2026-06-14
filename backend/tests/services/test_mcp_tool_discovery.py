"""J — MCP server tools are discoverable through the unified ``tool_search``.

``list_agent_mcp_deferred_tools`` is the single DB-aware enumerator both the
text result (``workspace._tool_search``) and the schema injection
(``invoker._deferred_tool_names_for_query``) route through (🦴#2), so what the
model is told exists equals what actually loads. Its listing gate must mirror
the schema-load gate: reachable only, denied/disabled excluded, and — critically
— discovery must NOT be able to force-enable a non-default MCP tool (🦴#1).

Follows the established ``_FakeSession`` queued-results convention for this code
path (see ``test_agent_mcp_gating.py`` — the governance logic runs for real; only
the session is faked, exactly as every other test of this surface).
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


def _mcp_tool(*, name, tool_id, is_default=False):
    return SimpleNamespace(
        id=tool_id,
        name=name,
        display_name=name,
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
        mcp_tool_name=name,
    )


def _mcp_tool_for_tenant(*, name, tool_id, tenant_id, is_default=False):
    tool = _mcp_tool(name=name, tool_id=tool_id, is_default=is_default)
    tool.tenant_id = tenant_id
    return tool


def _assignment(*, agent_id, server_id, enabled=True, default_tool_mode="auto"):
    return SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        mcp_server_id=server_id,
        enabled=enabled,
        default_tool_mode=default_tool_mode,
        always_load=False,
    )


def _server_tool(*, server_id, tool_id, tool_name):
    return SimpleNamespace(id=uuid4(), mcp_server_id=server_id, tool_id=tool_id, mcp_tool_name=tool_name)


def _override(*, agent_id, server_id, tool_name, mode):
    return SimpleNamespace(id=uuid4(), agent_id=agent_id, mcp_server_id=server_id, tool_name=tool_name, mode=mode)


def _patch_pack_open(monkeypatch, module):
    monkeypatch.setattr(module, "is_pack_enabled", lambda _policies, _pack: True)

    # RLS 阶段1: list_agent_mcp_deferred_tools / get_agent_tools_for_llm now
    # resolve the agent's tenant and open a tenant-scoped session. Route
    # tenant_scoped_session through whatever ``async_session`` factory the test
    # already installed (no real SET LOCAL → the queued result sequence is
    # intact) and stub the tenant resolver. agent_tools imports both at module
    # level. Called after the per-test ``async_session`` patch, so capture it
    # lazily at call time.
    async def _resolve(*_a, **_k):
        return uuid4()

    monkeypatch.setattr(module, "tenant_scoped_session", lambda *_a, **_k: module.async_session(), raising=False)
    monkeypatch.setattr(module, "resolve_tenant_for_agent", _resolve, raising=False)


# --- list_agent_mcp_deferred_tools: the listing gate ----------------------


@pytest.mark.asyncio
async def test_discovery_lists_reachable_mcp_tool(monkeypatch):
    from app.services import agent_tools as module

    agent_id, tenant_id, server = uuid4(), uuid4(), uuid4()
    t = _mcp_tool(name="issue_search", tool_id=uuid4())

    def factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ListResult([_assignment(agent_id=agent_id, server_id=server)]),
                _ListResult([_server_tool(server_id=server, tool_id=t.id, tool_name="issue_search")]),
                _ListResult([]),  # overrides
                _ListResult([t]),  # mcp tools
                _ListResult([]),  # legacy AgentTool
            ]
        )

    # RLS 阶段2b: the bare async_session import was removed from agent_tools; this
    # test routes its fake DB through it via _patch_pack_open's tenant_scoped_session
    # shim, so create the attribute (raising=False) for that shim to read.
    monkeypatch.setattr(module, "async_session", factory, raising=False)
    _patch_pack_open(monkeypatch, module)

    names = await module.list_agent_mcp_deferred_tools(agent_id, "issue")
    assert names == ["issue_search"]


@pytest.mark.asyncio
async def test_discovery_excludes_denied_tool(monkeypatch):
    from app.services import agent_tools as module

    agent_id, tenant_id, server = uuid4(), uuid4(), uuid4()
    keep = _mcp_tool(name="issue_search", tool_id=uuid4())
    denied = _mcp_tool(name="repo_delete", tool_id=uuid4())

    def factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ListResult([_assignment(agent_id=agent_id, server_id=server)]),
                _ListResult(
                    [
                        _server_tool(server_id=server, tool_id=keep.id, tool_name="issue_search"),
                        _server_tool(server_id=server, tool_id=denied.id, tool_name="repo_delete"),
                    ]
                ),
                _ListResult([_override(agent_id=agent_id, server_id=server, tool_name="repo_delete", mode="deny")]),
                _ListResult([keep, denied]),
                _ListResult([]),
            ]
        )

    # RLS 阶段2b: the bare async_session import was removed from agent_tools; this
    # test routes its fake DB through it via _patch_pack_open's tenant_scoped_session
    # shim, so create the attribute (raising=False) for that shim to read.
    monkeypatch.setattr(module, "async_session", factory, raising=False)
    _patch_pack_open(monkeypatch, module)

    names = await module.list_agent_mcp_deferred_tools(agent_id, "")
    assert "issue_search" in names
    assert "repo_delete" not in names  # deny override → never surfaced


@pytest.mark.asyncio
async def test_discovery_excludes_disabled_server(monkeypatch):
    from app.services import agent_tools as module

    agent_id, tenant_id, server = uuid4(), uuid4(), uuid4()
    t = _mcp_tool(name="issue_search", tool_id=uuid4())

    def factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ListResult([_assignment(agent_id=agent_id, server_id=server, enabled=False)]),
                _ListResult([_server_tool(server_id=server, tool_id=t.id, tool_name="issue_search")]),
                _ListResult([]),
                _ListResult([t]),
                _ListResult([]),
            ]
        )

    # RLS 阶段2b: the bare async_session import was removed from agent_tools; this
    # test routes its fake DB through it via _patch_pack_open's tenant_scoped_session
    # shim, so create the attribute (raising=False) for that shim to read.
    monkeypatch.setattr(module, "async_session", factory, raising=False)
    _patch_pack_open(monkeypatch, module)

    names = await module.list_agent_mcp_deferred_tools(agent_id, "")
    assert names == []  # disabled assignment → not reachable → not listed


@pytest.mark.asyncio
async def test_discovery_legacy_fallback_lists_default_not_nondefault(monkeypatch):
    """Un-backfilled agent: only is_default MCP tools surface; a non-default one
    with no assignment is invisible to discovery (the 🦴#1 choke point)."""
    from app.services import agent_tools as module

    agent_id, tenant_id = uuid4(), uuid4()
    default_tool = _mcp_tool(name="default_search", tool_id=uuid4(), is_default=True)
    nondefault_tool = _mcp_tool(name="danger_delete", tool_id=uuid4(), is_default=False)

    def factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ListResult([]),  # no assignments → _resolve_agent_mcp_gating returns None (un-backfilled)
                _ListResult([default_tool, nondefault_tool]),
                _ListResult([]),  # no legacy AgentTool rows
            ]
        )

    # RLS 阶段2b: the bare async_session import was removed from agent_tools; this
    # test routes its fake DB through it via _patch_pack_open's tenant_scoped_session
    # shim, so create the attribute (raising=False) for that shim to read.
    monkeypatch.setattr(module, "async_session", factory, raising=False)
    _patch_pack_open(monkeypatch, module)

    names = await module.list_agent_mcp_deferred_tools(agent_id, "")
    assert "default_search" in names
    assert "danger_delete" not in names


@pytest.mark.asyncio
async def test_discovery_excludes_other_tenant_default_mcp(monkeypatch):
    """A default MCP tool from another tenant must not leak into tool_search."""
    from app.services import agent_tools as module

    agent_id, tenant_id, other_tenant_id = uuid4(), uuid4(), uuid4()
    local_default = _mcp_tool_for_tenant(
        name="local_default_search",
        tool_id=uuid4(),
        tenant_id=tenant_id,
        is_default=True,
    )
    foreign_default = _mcp_tool_for_tenant(
        name="foreign_default_search",
        tool_id=uuid4(),
        tenant_id=other_tenant_id,
        is_default=True,
    )

    def factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ListResult([]),  # no MCP assignments -> legacy default fallback
                _ListResult([local_default, foreign_default]),
                _ListResult([]),  # no legacy AgentTool rows
            ]
        )

    # RLS 阶段2b: the bare async_session import was removed from agent_tools; this
    # test routes its fake DB through it via _patch_pack_open's tenant_scoped_session
    # shim, so create the attribute (raising=False) for that shim to read.
    monkeypatch.setattr(module, "async_session", factory, raising=False)
    _patch_pack_open(monkeypatch, module)

    names = await module.list_agent_mcp_deferred_tools(agent_id, "default")
    assert "local_default_search" in names
    assert "foreign_default_search" not in names


# --- 🦴#1: discovery cannot force-enable a non-default MCP tool -------------


@pytest.mark.asyncio
async def test_discovery_cannot_force_enable_nondefault_mcp(monkeypatch):
    """Requesting a non-default MCP tool by name (as tool_search discovery would)
    must NOT enable it for an un-backfilled agent — that was a privilege-escalation
    path through the ``explicit_requested_set`` clause."""
    from app.services import agent_tools as module

    agent_id, tenant_id = uuid4(), uuid4()
    nondefault = _mcp_tool(name="danger_delete", tool_id=uuid4(), is_default=False)

    async def no_feishu_channel(_agent_id):
        return False

    async def no_feishu_cli():
        return False

    async def passthrough(_agent_id, tools):
        return tools

    monkeypatch.setattr(module, "_agent_has_feishu", no_feishu_channel)
    monkeypatch.setattr(module, "_agent_has_feishu_cli_access", no_feishu_cli)
    monkeypatch.setattr(module, "_filter_unavailable_tools", passthrough)
    _patch_pack_open(monkeypatch, module)

    def factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ScalarResult(None),
                _ListResult([nondefault]),  # all_tools
                _ListResult([]),  # NO AgentTool rows
                _ListResult([]),  # _resolve_agent_mcp_gating: no assignments → None (un-backfilled)
            ]
        )

    # RLS 阶段2b: async_session import removed; routed via _patch_pack_open shim.
    monkeypatch.setattr(module, "async_session", factory, raising=False)

    tools = await module.get_agent_tools_for_llm(agent_id, requested_names=["danger_delete"])
    names = {t["function"]["name"] for t in tools}
    assert "danger_delete" not in names  # discovery alone must not enable it


@pytest.mark.asyncio
async def test_schema_load_excludes_other_tenant_default_mcp(monkeypatch):
    """The callable-schema path must mirror text discovery and exclude other tenants."""
    from app.services import agent_tools as module

    agent_id, tenant_id, other_tenant_id = uuid4(), uuid4(), uuid4()
    local_default = _mcp_tool_for_tenant(
        name="local_schema_search",
        tool_id=uuid4(),
        tenant_id=tenant_id,
        is_default=True,
    )
    foreign_default = _mcp_tool_for_tenant(
        name="foreign_schema_search",
        tool_id=uuid4(),
        tenant_id=other_tenant_id,
        is_default=True,
    )

    async def no_feishu_channel(_agent_id):
        return False

    async def no_feishu_cli():
        return False

    async def passthrough(_agent_id, tools):
        return tools

    monkeypatch.setattr(module, "_agent_has_feishu", no_feishu_channel)
    monkeypatch.setattr(module, "_agent_has_feishu_cli_access", no_feishu_cli)
    monkeypatch.setattr(module, "_filter_unavailable_tools", passthrough)
    _patch_pack_open(monkeypatch, module)

    def factory():
        return _FakeSession(
            [
                _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard")),
                _ScalarResult(None),
                _ListResult([local_default, foreign_default]),  # all_tools
                _ListResult([]),  # no AgentTool rows
                _ListResult([]),  # _resolve_agent_mcp_gating -> un-backfilled
            ]
        )

    # RLS 阶段2b: async_session import removed; routed via _patch_pack_open shim.
    monkeypatch.setattr(module, "async_session", factory, raising=False)

    tools = await module.get_agent_tools_for_llm(
        agent_id,
        requested_names=["local_schema_search", "foreign_schema_search"],
    )
    names = {t["function"]["name"] for t in tools}
    assert "local_schema_search" in names
    assert "foreign_schema_search" not in names


# --- 🦴#2: text and schema surfaces agree on MCP ---------------------------


@pytest.mark.asyncio
async def test_tool_search_text_and_schema_agree_on_mcp(monkeypatch, tmp_path):
    """Both the text result (_tool_search) and the schema path
    (_deferred_tool_names_for_query) route through the SAME single source
    (``agent_tools.discoverable_tool_names_for_query``), so the model is never told
    one set of MCP tools while a different set loads (Step 3 / 🦴#2)."""
    import app.runtime.invoker as invoker
    import app.services.agent_tools as agent_tools
    from app.services.agent_tool_domains.workspace import _tool_search

    agent_id = uuid4()

    async def fake_enumerator(_agent_id, _query):
        return ["mcp_github_issue_search"]

    # Step 3 unified the two surfaces: both route through
    # discoverable_tool_names_for_query, which reads the MCP enumerator from the
    # agent_tools module global at call time. Patching the single source proves the
    # two surfaces can no longer diverge — one patch point covers both.
    monkeypatch.setattr(agent_tools, "list_agent_mcp_deferred_tools", fake_enumerator)

    text = await _tool_search(tmp_path, "github", agent_id=agent_id)
    schema_names = await invoker._deferred_tool_names_for_query(agent_id, "github")

    assert "mcp_github_issue_search" in text
    assert "mcp_github_issue_search" in schema_names


# --- dynamic MCP execution fallback: tenant + reachability -----------------


class _RoutingSession:
    def __init__(self, *, agent, tool, agent_tool=None, server_tools=None):
        self.agent = agent
        self.tool = tool
        self.agent_tool = agent_tool
        self.server_tools = server_tools or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, stmt):
        text = str(stmt)
        if "FROM agents" in text:
            return _ScalarResult(self.agent)
        if "FROM tools" in text and "agent_tools" not in text:
            return _ScalarResult(self.tool)
        if "FROM agent_tools" in text:
            return _ScalarResult(self.agent_tool)
        if "FROM mcp_server_tools" in text:
            return _ListResult(self.server_tools)
        return _ListResult([])


@pytest.mark.asyncio
async def test_dynamic_mcp_fallback_rejects_other_tenant_tool(monkeypatch):
    """A callable MCP name must not execute a Tool row owned by another tenant."""
    from app.services.agent_tool_domains import web_mcp

    agent_id, tenant_id, other_tenant_id = uuid4(), uuid4(), uuid4()
    foreign_tool = _mcp_tool_for_tenant(
        name="foreign_exec_search",
        tool_id=uuid4(),
        tenant_id=other_tenant_id,
        is_default=True,
    )

    def _routing_session(*_a, **_k):
        return _RoutingSession(
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id, agent_class="standard"),
            tool=foreign_tool,
            agent_tool=None,
            server_tools=[],
        )

    monkeypatch.setattr(web_mcp, "async_session", _routing_session)

    # RLS 阶段1: _execute_mcp_tool resolves the agent's tenant then opens a
    # tenant-scoped session. Route tenant_scoped_session through the same fake
    # routing session and stub the resolver (web_mcp imports both at module
    # level). The Python-side cross-tenant rejection under test is unchanged.
    async def _resolve(*_a, **_k):
        return tenant_id

    monkeypatch.setattr(web_mcp, "tenant_scoped_session", _routing_session, raising=False)
    monkeypatch.setattr(web_mcp, "resolve_tenant_for_agent", _resolve, raising=False)

    calls: list[tuple] = []

    class _FakeMCPClient:
        def __init__(self, *args, **kwargs):
            calls.append(("init", args, kwargs))

        async def call_tool(self, *args, **kwargs):
            calls.append(("call", args, kwargs))
            return "CALLED"

    monkeypatch.setattr("app.services.mcp_client.MCPClient", _FakeMCPClient)

    result = await web_mcp._execute_mcp_tool("foreign_exec_search", {"q": "x"}, agent_id=agent_id)

    assert calls == []
    assert "Unknown tool" in result or "not imported" in result or "denied" in result
