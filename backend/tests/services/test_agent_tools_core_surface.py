"""T1 core tool surface invariants (docs/execution-mode-spectrum.md §4.6 / §8.1).

T1.1 atomic pair (red tests #1/#3/#7): the source capabilities
``spawn_subagent`` / ``propose_dynamic_workflow`` / ``preview_workflow`` / ``start_workflow`` are promoted to
``CORE_TOOL_NAMES`` (turn-1 visible via the ``_always_tools`` fallback), and —
in the SAME commit — both recursion exclusion sets deny them. Once the tools
are in core, ``core_tools_only=True`` child profiles would otherwise leak them
into child tool surfaces (the pack gate that passively blocked them is no
longer in the path).

T1.2 (red test #2): the work-ledger trio ``track_todo`` / ``record_finding`` /
``read_ledger`` joins ``CORE_TOOL_NAMES`` — working memory is an agent
thinking tool and must not depend on DB ``Tool.is_default``/assignment to
exist. The ``should_enable_work_ledger`` reminder gate is untouched: it
governs reminder frequency (session metadata), never tool visibility.
"""

from __future__ import annotations

SOURCE_CAPABILITY_TOOLS = {"spawn_subagent", "propose_dynamic_workflow", "preview_workflow", "start_workflow"}
WORK_LEDGER_TOOLS = {"track_todo", "record_finding", "read_ledger"}
ADVANCED_WEB_TOOLS = {"exa_search", "tavily_search", "firecrawl_fetch", "xcrawl_scrape"}
CODING_PLUGIN_TOOLS = {"lsp_symbol_search", "worktree_create", "notebook_edit", "browser_ui_open"}


# ── Red test #1 — turn-1 visibility ─────────────────────────────────


def test_core_tool_names_include_source_capabilities():
    from app.services.agent_tools import CORE_TOOL_NAMES

    assert SOURCE_CAPABILITY_TOOLS <= CORE_TOOL_NAMES


def test_collected_surface_provides_schemas_for_core_source_capabilities():
    """The ``_always_tools`` fallback serves schemas from the collected surface:
    a core membership without a collected schema would be silently invisible."""
    from app.services.agent_tools import CORE_TOOL_NAMES, get_combined_openai_tools

    combined_names = {t["function"]["name"] for t in get_combined_openai_tools()}
    assert SOURCE_CAPABILITY_TOOLS <= (combined_names & CORE_TOOL_NAMES)


# ── Red test #2 (T1.2) — work ledger is core, not DB-conditional ────


def test_core_tool_names_include_work_ledger():
    from app.services.agent_tools import CORE_TOOL_NAMES

    assert WORK_LEDGER_TOOLS <= CORE_TOOL_NAMES


def test_collected_surface_provides_schemas_for_core_ledger_tools():
    """Core membership grants the ``_always_tools`` fallback: availability no
    longer depends on DB ``Tool.is_default``/assignment rows."""
    from app.services.agent_tools import CORE_TOOL_NAMES, get_combined_openai_tools

    combined_names = {t["function"]["name"] for t in get_combined_openai_tools()}
    assert WORK_LEDGER_TOOLS <= (combined_names & CORE_TOOL_NAMES)


# ── Red test #3 — recursion guard, BOTH paths ───────────────────────


def test_subagent_base_exclusions_deny_source_capabilities():
    from app.agents.subagent import _SUBAGENT_BASE_EXCLUDED_TOOLS

    assert SOURCE_CAPABILITY_TOOLS <= set(_SUBAGENT_BASE_EXCLUDED_TOOLS)


def test_resolve_subagent_tools_excludes_source_capabilities_for_all_types():
    """Every subagent type — including unknown/custom types whose empty
    allow-list means "all core tools" downstream — must carry the base
    exclusions for the three source capabilities."""
    from app.agents.subagent import SubagentSpec, resolve_subagent_tools

    for subagent_type in ("explorer", "worker", "critic", "custom-profile"):
        spec = SubagentSpec(name="t1-probe", type=subagent_type)
        excluded = resolve_subagent_tools(spec)[1]
        assert SOURCE_CAPABILITY_TOOLS <= set(excluded), subagent_type


def test_delegation_base_exclusions_deny_source_capabilities():
    from app.agents.orchestrator import _DELEGATION_BASE_EXCLUDED_TOOLS

    assert SOURCE_CAPABILITY_TOOLS <= set(_DELEGATION_BASE_EXCLUDED_TOOLS)


def test_delegation_and_subagent_share_one_base_exclusion_policy():
    """Child-agent recursion/control exclusions must have one source of truth."""
    from app.agents.orchestrator import _DELEGATION_BASE_EXCLUDED_TOOLS
    from app.agents.subagent import _SUBAGENT_BASE_EXCLUDED_TOOLS
    from app.agents.tool_policies import DELEGATED_WORKER_BASE_EXCLUDED_TOOLS

    assert _SUBAGENT_BASE_EXCLUDED_TOOLS == DELEGATED_WORKER_BASE_EXCLUDED_TOOLS
    assert _DELEGATION_BASE_EXCLUDED_TOOLS == DELEGATED_WORKER_BASE_EXCLUDED_TOOLS
    for tool_name in (
        "delegate_to_agent",
        "send_message_to_agent",
        "spawn_subagent",
        "check_subagent",
        "fanout_subagents",
        "propose_dynamic_workflow",
        "preview_workflow",
        "start_workflow",
        "ask_user_question",
        "request_plan_mode",
    ):
        assert tool_name in DELEGATED_WORKER_BASE_EXCLUDED_TOOLS


def test_delegation_profiles_never_grant_source_capabilities():
    """Behavioural pin: no delegation profile's effective tool-name set may
    contain a source capability — covers both ``core_tools_only`` subtraction
    profiles (worker_safe, memory_readonly) and allowlist profiles
    (review_readonly, research_readonly)."""
    from app.agents.orchestrator import (
        _DELEGATION_TOOL_PROFILES,
        _delegation_profile_tool_names,
    )

    for profile in _DELEGATION_TOOL_PROFILES.values():
        effective = _delegation_profile_tool_names(profile)
        leaked = SOURCE_CAPABILITY_TOOLS & effective
        assert not leaked, f"{profile.name} leaks {leaked}"


# ── Step 0 — CORE-only pack retired, no catalog straddle ─────────────


def test_coordination_pack_retired_source_capabilities_stay_core():
    """Step 0: the zero-effect ``coordination_pack`` catalog anchor is retired.
    Its members were all CORE (turn-1 visible via the ``_always_tools`` fallback,
    bypassing pack policy), so the pack entry added no discovery signal — only a
    drifting second source of truth. The source capabilities remain CORE."""
    from app.services.agent_tools import CORE_TOOL_NAMES
    from app.tools.runtime_tool_groups import runtime_tool_group_for_name

    assert runtime_tool_group_for_name("coordination_pack") is None
    assert SOURCE_CAPABILITY_TOOLS <= CORE_TOOL_NAMES


def test_web_search_core_but_advanced_web_tools_deferred():
    """Basic web search is turn-1 core; provider-backed search/crawlers are
    discovered through tool_search so the model chooses when to escalate."""
    from app.services.agent_tools import CORE_TOOL_NAMES
    from app.tools.runtime_tool_groups import runtime_tool_group_for_name

    web_pack = runtime_tool_group_for_name("web_pack")

    assert "web_search" in CORE_TOOL_NAMES
    assert web_pack is not None
    assert ADVANCED_WEB_TOOLS <= set(web_pack.tools)
    assert "web_search" not in set(web_pack.tools)
    assert not (ADVANCED_WEB_TOOLS & CORE_TOOL_NAMES)


def test_governance_capability_taxonomy_is_single_source_for_core_and_l2():
    from app.services.agent_tools import CORE_TOOL_NAMES
    from app.services.governance_capability_taxonomy import (
        GovernanceCapabilityLayer,
        capability_descriptor_for_tool,
        iter_l2_capabilities,
    )

    for tool_name in CORE_TOOL_NAMES:
        descriptor = capability_descriptor_for_tool(tool_name)
        assert descriptor is not None, tool_name
        assert descriptor.layer == GovernanceCapabilityLayer.AGENT_BASE.value, tool_name
        assert descriptor.l2_visible is False, tool_name
        assert descriptor.enterprise_toggleable is False, tool_name

    l2_tools = {tool for descriptor in iter_l2_capabilities() for tool in descriptor.tools}
    assert ADVANCED_WEB_TOOLS <= l2_tools
    assert "send_feishu_message" in l2_tools
    assert "call_mcp_tool" in l2_tools
    assert not (CORE_TOOL_NAMES & l2_tools)


def test_coding_capability_is_l2_local_bridge_only():
    from app.services.governance_capability_taxonomy import (
        GovernanceCapabilityLayer,
        capability_descriptor_for_name,
    )

    descriptor = capability_descriptor_for_name("coding")

    assert descriptor is not None
    assert descriptor.layer == GovernanceCapabilityLayer.EXTERNAL_EXTENSION.value
    assert descriptor.l2_visible is True
    assert descriptor.enterprise_toggleable is True
    assert descriptor.requires_local_bridge is True
    assert CODING_PLUGIN_TOOLS <= set(descriptor.tools)
