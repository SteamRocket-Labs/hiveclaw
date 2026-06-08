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
                _ScalarResult(None),  # pack policy
                _ListResult([_assignment(agent_id=agent_id, server_id=server)]),
                _ListResult([_server_tool(server_id=server, tool_id=t.id, tool_name="issue_search")]),
                _ListResult([]),  # overrides
                _ListResult([t]),  # mcp tools
                _ListResult([]),  # legacy AgentTool
            ]
        )

    monkeypatch.setattr(module, "async_session", factory)
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
                _ScalarResult(None),
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

    monkeypatch.setattr(module, "async_session", factory)
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
                _ScalarResult(None),
                _ListResult([_assignment(agent_id=agent_id, server_id=server, enabled=False)]),
                _ListResult([_server_tool(server_id=server, tool_id=t.id, tool_name="issue_search")]),
                _ListResult([]),
                _ListResult([t]),
                _ListResult([]),
            ]
        )

    monkeypatch.setattr(module, "async_session", factory)
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
                _ScalarResult(None),
                _ListResult([]),  # no assignments → _resolve_agent_mcp_gating returns None (un-backfilled)
                _ListResult([default_tool, nondefault_tool]),
                _ListResult([]),  # no legacy AgentTool rows
            ]
        )

    monkeypatch.setattr(module, "async_session", factory)
    _patch_pack_open(monkeypatch, module)

    names = await module.list_agent_mcp_deferred_tools(agent_id, "")
    assert "default_search" in names
    assert "danger_delete" not in names


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

    monkeypatch.setattr(module, "async_session", factory)

    tools = await module.get_agent_tools_for_llm(agent_id, requested_names=["danger_delete"])
    names = {t["function"]["name"] for t in tools}
    assert "danger_delete" not in names  # discovery alone must not enable it


# --- 🦴#2: text and schema surfaces agree on MCP ---------------------------


@pytest.mark.asyncio
async def test_tool_search_text_and_schema_agree_on_mcp(monkeypatch, tmp_path):
    """Both the text result (_tool_search) and the schema path
    (_deferred_tool_names_for_query) route through the same enumerator, so the
    model is never told one set of MCP tools while a different set loads."""
    import app.runtime.invoker as invoker
    import app.services.agent_tools as agent_tools
    from app.services.agent_tool_domains.workspace import _tool_search

    agent_id = uuid4()

    async def fake_enumerator(_agent_id, _query):
        return ["mcp_github_issue_search"]

    # workspace imports the enumerator from agent_tools at call time; invoker binds
    # it at import time — patch both so each surface uses the shared list.
    monkeypatch.setattr(agent_tools, "list_agent_mcp_deferred_tools", fake_enumerator)
    monkeypatch.setattr(invoker, "list_agent_mcp_deferred_tools", fake_enumerator)

    text = await _tool_search(tmp_path, "github", agent_id=agent_id)
    schema_names = await invoker._deferred_tool_names_for_query(agent_id, "github")

    assert "mcp_github_issue_search" in text
    assert "mcp_github_issue_search" in schema_names
