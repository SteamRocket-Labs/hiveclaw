"""Step 0 invariants: CORE∩pack disjoint + CORE as a tool-discovery path.

Retiring the CORE-only ``coordination_pack`` / ``plan_mode_pack`` catalog
anchors and clearing the CORE-tool straddlers (web_fetch / read_file /
list_files / send_channel_file) must leave no CORE tool listed as a pack
member, and the startup audit must treat CORE membership as a discovery path
so retiring those packs does not mislabel core tools as functional orphans.
"""

from __future__ import annotations

import pytest


def test_core_pack_disjoint_invariant_holds():
    """The hard startup invariant passes on the shipped tool surface."""
    from app.tools.audit import assert_core_pack_disjoint

    assert_core_pack_disjoint()  # must not raise


def test_assert_core_pack_disjoint_raises_on_straddle(monkeypatch):
    """A CORE tool that also sits in a RUNTIME_TOOL_GROUPS pack must fail the
    invariant (fail-fast). firecrawl_fetch is a real web_pack member; pretend it
    became CORE."""
    import app.services.agent_tools as agent_tools
    import app.tools.audit as audit

    monkeypatch.setattr(agent_tools, "CORE_TOOL_NAMES", agent_tools.CORE_TOOL_NAMES | {"firecrawl_fetch"})
    with pytest.raises(RuntimeError, match="CORE∩pack invariant violated"):
        audit.assert_core_pack_disjoint()


def test_no_core_tool_listed_as_pack_member():
    from app.services.agent_tools import CORE_TOOL_NAMES
    from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS

    pack_tools = {tool for pack in RUNTIME_TOOL_GROUPS for tool in pack.tools}
    assert not (CORE_TOOL_NAMES & pack_tools)


def test_straddler_tools_removed_from_packs():
    """web_fetch / read_file / list_files / send_channel_file are CORE and must
    no longer appear in any RUNTIME_TOOL_GROUPS member list."""
    from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS

    pack_tools = {tool for pack in RUNTIME_TOOL_GROUPS for tool in pack.tools}
    for straddler in ("web_fetch", "read_file", "list_files", "send_channel_file"):
        assert straddler not in pack_tools, straddler


def test_retired_packs_absent():
    from app.tools.runtime_tool_groups import runtime_tool_group_for_name

    assert runtime_tool_group_for_name("coordination_pack") is None
    assert runtime_tool_group_for_name("plan_mode_pack") is None


def test_audit_treats_core_membership_as_discovery_path():
    """Source capabilities (CORE, no longer in any pack) must be covered by the
    audit via the covered_by_core path — never flagged as functional orphans."""
    from app.services.agent_tools import CORE_TOOL_NAMES
    from app.tools.audit import audit_tool_coverage

    report = audit_tool_coverage()
    assert report.covered_by_core, "covered_by_core should be non-empty"
    # No CORE tool may be flagged as a functional orphan.
    assert not (report.orphans & CORE_TOOL_NAMES)
    # The retired-pack source capabilities remain covered (via the core path).
    for tool in ("spawn_subagent", "start_workflow", "request_plan_mode", "web_fetch"):
        assert tool not in report.orphans, tool
