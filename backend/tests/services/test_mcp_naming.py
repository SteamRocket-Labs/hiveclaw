"""Tests for canonical MCP tool naming (Step 6)."""

from __future__ import annotations

from app.services.mcp_naming import (
    MAX_MCP_TOOL_NAME_LEN,
    McpNameRow,
    build_mcp_tool_name,
    is_mcp_tool_name,
    parse_mcp_tool_name,
    plan_mcp_name_canonicalization,
)


def test_build_basic_canonical_name():
    assert build_mcp_tool_name("GitHub", "issue_search") == "mcp__github__issue-search"


def test_build_slugifies_both_segments():
    # Non-alphanumeric runs collapse to '-'; never to '_' (so '__' stays a clean separator).
    name = build_mcp_tool_name("My.Server v2", "get/screen size")
    assert name == "mcp__my-server-v2__get-screen-size"
    assert "__" in name
    # The only '__' occurrences are the prefix and the single separator.
    assert name.count("__") == 1 + 1  # mcp__ prefix + the server/tool separator


def test_parse_round_trips_to_slugs():
    name = build_mcp_tool_name("GitHub", "issue_search")
    assert parse_mcp_tool_name(name) == ("github", "issue-search")


def test_parse_rejects_non_canonical():
    assert parse_mcp_tool_name("web_search") is None
    assert parse_mcp_tool_name("mcp_github_issue_search") is None  # legacy single-underscore
    assert parse_mcp_tool_name(None) is None
    assert parse_mcp_tool_name("mcp__only_server") is None  # no separator after prefix


def test_is_mcp_tool_name():
    assert is_mcp_tool_name("mcp__github__issue-search") is True
    assert is_mcp_tool_name("mcp_github_issue_search") is False
    assert is_mcp_tool_name("web_search") is False
    assert is_mcp_tool_name(None) is False


def test_empty_inputs_get_safe_placeholders():
    # slugify falls back to "server"; tool falls back to "server" too (both via slugify).
    name = build_mcp_tool_name(None, None)
    assert is_mcp_tool_name(name)
    assert len(name) <= MAX_MCP_TOOL_NAME_LEN


def test_length_cap_with_deterministic_hash():
    long_server = "a-very-long-mcp-server-name-that-keeps-going-and-going"
    long_tool = "an-equally-long-remote-tool-name-that-also-keeps-going"
    name1 = build_mcp_tool_name(long_server, long_tool)
    name2 = build_mcp_tool_name(long_server, long_tool)
    assert len(name1) <= MAX_MCP_TOOL_NAME_LEN
    assert name1 == name2  # deterministic
    assert is_mcp_tool_name(name1)
    # Different identities must not collide after truncation.
    other = build_mcp_tool_name(long_server, long_tool + "-x")
    assert other != name1


def test_taken_disambiguates_slug_collisions():
    taken: set[str] = set()
    a = build_mcp_tool_name("GitHub", "search", taken=taken)
    taken.add(a)
    b = build_mcp_tool_name("git.hub", "search", taken=taken)  # slugs to same base
    assert a == "mcp__github__search"
    assert b != a
    assert b not in (None, "")
    assert len(b) <= MAX_MCP_TOOL_NAME_LEN


def test_charset_is_provider_safe():
    name = build_mcp_tool_name("Wëird Nämé!!", "do@thing#now")
    assert all(c.isalnum() or c in "_-" for c in name)
    assert len(name) <= MAX_MCP_TOOL_NAME_LEN


# ── plan_mcp_name_canonicalization ────────────────────────────────────────────


def test_plan_renames_legacy_names():
    rows = [
        McpNameRow(tool_id="t1", name="mcp_github_issue_search", mcp_server_name="GitHub", mcp_tool_name="issue_search"),
    ]
    plan = plan_mcp_name_canonicalization(rows)
    assert len(plan) == 1
    assert plan[0].old_name == "mcp_github_issue_search"
    assert plan[0].new_name == "mcp__github__issue-search"


def test_plan_skips_already_canonical():
    rows = [
        McpNameRow(tool_id="t1", name="mcp__github__issue-search", mcp_server_name="GitHub", mcp_tool_name="issue_search"),
    ]
    assert plan_mcp_name_canonicalization(rows) == []


def test_plan_disambiguates_collision_within_tenant():
    # Two servers slugging to the same base, same tool -> the second is suffixed.
    rows = [
        McpNameRow(tool_id="t1", name="legacy_a", mcp_server_name="GitHub", mcp_tool_name="search", tenant_id="x"),
        McpNameRow(tool_id="t2", name="legacy_b", mcp_server_name="git.hub", mcp_tool_name="search", tenant_id="x"),
    ]
    plan = plan_mcp_name_canonicalization(rows)
    new_names = {r.tool_id: r.new_name for r in plan}
    assert new_names["t1"] == "mcp__github__search"
    assert new_names["t2"] != new_names["t1"]
    assert len({r.new_name for r in plan}) == 2  # unique within tenant


def test_plan_reserves_canonical_before_renaming_sibling():
    # t1 already canonical; a legacy sibling that wants the same name must not steal it.
    rows = [
        McpNameRow(tool_id="t1", name="mcp__github__search", mcp_server_name="GitHub", mcp_tool_name="search", tenant_id="x"),
        McpNameRow(tool_id="t2", name="legacy", mcp_server_name="git.hub", mcp_tool_name="search", tenant_id="x"),
    ]
    plan = plan_mcp_name_canonicalization(rows)
    assert [r.tool_id for r in plan] == ["t2"]  # t1 untouched
    assert plan[0].new_name != "mcp__github__search"


def test_plan_same_name_different_tenants_no_collision():
    rows = [
        McpNameRow(tool_id="t1", name="legacy_a", mcp_server_name="GitHub", mcp_tool_name="search", tenant_id="x"),
        McpNameRow(tool_id="t2", name="legacy_b", mcp_server_name="GitHub", mcp_tool_name="search", tenant_id="y"),
    ]
    plan = plan_mcp_name_canonicalization(rows)
    # Each tenant independently gets the clean canonical name (no cross-tenant suffix).
    assert {r.new_name for r in plan} == {"mcp__github__search"}
