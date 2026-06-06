"""T1 core tool surface invariants (docs/execution-mode-spectrum.md §4.6 / §8.1).

T1.1 atomic pair (red tests #1/#3/#7): the source capabilities
``spawn_subagent`` / ``preview_workflow`` / ``start_workflow`` are promoted to
``CORE_TOOL_NAMES`` (turn-1 visible via the ``_always_tools`` fallback), and —
in the SAME commit — both recursion exclusion sets deny them. Once the tools
are in core, ``core_tools_only=True`` child profiles would otherwise leak them
into child tool surfaces (the pack gate that passively blocked them is no
longer in the path).
"""

from __future__ import annotations

SOURCE_CAPABILITY_TOOLS = {"spawn_subagent", "preview_workflow", "start_workflow"}


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


# ── Red test #7 — pack keeps catalog semantics ──────────────────────


def test_coordination_pack_remains_catalog_for_source_capabilities():
    """The pack stays as a directory/grouping anchor; promotion to core does
    not delete it. (Deliberately NOT asserting that disabling the pack hides
    the three tools — core members bypass pack policy via the
    ``_always_tools`` fallback, §8.2#1.)"""
    from app.tools.runtime_tool_groups import runtime_tool_group_for_name

    pack = runtime_tool_group_for_name("coordination_pack")
    assert pack is not None
    assert SOURCE_CAPABILITY_TOOLS <= set(pack.tools)
