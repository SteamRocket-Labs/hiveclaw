"""Part 2 — MCP backfill functional core + parity proofs.

The two parity properties are proven here as pure-function tests (no DB),
matching Hive's unit-first style:
  * Registry parity — group_mcp_tools reproduces build_mcp_server_registry's
    server name/url/tool set.
  * Tool-availability parity — for every agent, resolve_reachable_tools over the
    planned assignment equals the pre-migration reachable set. This is the
    cutover-day risk that registry parity cannot catch.
"""

from __future__ import annotations

from app.services.mcp_backfill import (
    AgentToolState,
    ServerSpec,
    derive_server_key,
    group_mcp_tools,
    plan_agent_assignment,
    reachable_before,
    resolve_reachable_tools,
)
from app.services.mcp_backfill import McpToolRow as Row


# ── grouping + server_key identity ─────────────────────────────


def test_group_mcp_tools_groups_by_name_and_url():
    specs = group_mcp_tools(
        [
            Row("t1", "issue_search", "GitHub: issue_search", "GitHub", "https://gh.tools"),
            Row("t2", "repo_read", "GitHub: repo_read", "GitHub", "https://gh.tools"),
        ]
    )
    assert len(specs) == 1
    assert specs[0].server_key == "github"
    assert specs[0].name == "GitHub"
    assert set(specs[0].tool_ids) == {"t1", "t2"}


def test_derive_server_key_disambiguates_same_name():
    taken: set[str] = set()
    k1 = derive_server_key("GitHub", "https://a", taken)
    taken.add(k1)
    k2 = derive_server_key("GitHub", "https://b", taken)
    assert k1 == "github"
    assert k2 == "github-2"


def test_server_key_never_uses_pack_namespace():
    specs = group_mcp_tools([Row("t1", "x", "X", "GitHub", "u")])
    assert not specs[0].server_key.startswith("mcp_server:")


# ── reachable_before: persisted half of get_agent_tools_for_llm ─


def test_reachable_before_uses_agenttool_enabled_when_row_exists():
    assert reachable_before(AgentToolState("a", "t", has_row=True, enabled=True), is_default=False) is True
    assert reachable_before(AgentToolState("a", "t", has_row=True, enabled=False), is_default=True) is False


def test_reachable_before_falls_back_to_is_default_when_no_row():
    assert reachable_before(None, is_default=True) is True
    assert reachable_before(None, is_default=False) is False


# ── gating contract (reused by Part 5) ─────────────────────────


def test_resolve_reachable_tools_disabled_server_yields_nothing():
    assert resolve_reachable_tools(["a", "b"], enabled=False, deny_tool_names=set()) == set()


def test_resolve_reachable_tools_enabled_minus_denies():
    assert resolve_reachable_tools(["a", "b", "c"], enabled=True, deny_tool_names={"b"}) == {"a", "c"}


# ── REGISTRY PARITY ────────────────────────────────────────────


def test_registry_parity_with_build_mcp_server_registry():
    from app.services.mcp_registry_service import build_mcp_server_registry

    legacy_rows = [
        {
            "tool_id": "t1",
            "tool_name": "issue_search",
            "display_name": "GH: issue",
            "mcp_server_name": "GitHub",
            "mcp_server_url": "https://gh",
            "agent_id": "a1",
            "agent_name": "Ops",
        },
        {
            "tool_id": "t2",
            "tool_name": "repo_read",
            "display_name": "GH: repo",
            "mcp_server_name": "GitHub",
            "mcp_server_url": "https://gh",
            "agent_id": "a2",
            "agent_name": "Research",
        },
    ]
    legacy = build_mcp_server_registry(legacy_rows)
    new = group_mcp_tools(
        [
            Row(r["tool_id"], r["tool_name"], r["display_name"], r["mcp_server_name"], r["mcp_server_url"])
            for r in legacy_rows
        ]
    )

    assert len(new) == len(legacy) == 1
    assert new[0].name == legacy[0]["server_name"]
    assert new[0].server_url == legacy[0]["server_url"]
    assert sorted(new[0].tool_names) == sorted(legacy[0]["tools"])


# ── TOOL-AVAILABILITY PARITY (the cutover-day risk) ────────────


def _reachable_set_before(server: ServerSpec, states, is_default) -> set[str]:
    return {
        name
        for tid, name in zip(server.tool_ids, server.tool_names)
        if reachable_before(states.get(tid), is_default[tid])
    }


def test_tool_availability_parity_mixed_enabled():
    server = ServerSpec("github", "GitHub", "u", ("t1", "t2", "t3"), ("alpha", "beta", "gamma"))
    states = {
        "t1": AgentToolState("a1", "t1", has_row=True, enabled=True),
        "t2": AgentToolState("a1", "t2", has_row=True, enabled=False),
    }
    is_default = {"t1": False, "t2": False, "t3": False}
    before = _reachable_set_before(server, states, is_default)
    assert before == {"alpha"}

    assignment = plan_agent_assignment(server, states, is_default, "a1")
    assert assignment is not None
    after = resolve_reachable_tools(list(server.tool_names), assignment.enabled, set(assignment.deny_tool_names))
    assert after == before


def test_tool_availability_parity_all_enabled_no_overrides():
    server = ServerSpec("github", "GitHub", "u", ("t1", "t2"), ("alpha", "beta"))
    states = {
        "t1": AgentToolState("a1", "t1", has_row=True, enabled=True),
        "t2": AgentToolState("a1", "t2", has_row=True, enabled=True),
    }
    is_default = {"t1": False, "t2": False}
    assignment = plan_agent_assignment(server, states, is_default, "a1")
    assert assignment.enabled is True
    assert assignment.deny_tool_names == ()
    after = resolve_reachable_tools(list(server.tool_names), assignment.enabled, set(assignment.deny_tool_names))
    assert after == {"alpha", "beta"}


def test_tool_availability_parity_all_disabled_yields_disabled_assignment():
    server = ServerSpec("github", "GitHub", "u", ("t1",), ("alpha",))
    states = {"t1": AgentToolState("a1", "t1", has_row=True, enabled=False)}
    assignment = plan_agent_assignment(server, states, {"t1": False}, "a1")
    assert assignment is not None
    assert assignment.enabled is False
    after = resolve_reachable_tools(list(server.tool_names), assignment.enabled, set(assignment.deny_tool_names))
    assert after == set()


def test_no_assignment_when_agent_unrelated_to_server():
    server = ServerSpec("github", "GitHub", "u", ("t1",), ("alpha",))
    assignment = plan_agent_assignment(server, {}, {"t1": False}, "a1")
    assert assignment is None
