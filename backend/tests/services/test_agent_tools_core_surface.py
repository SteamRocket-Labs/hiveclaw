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

import inspect
from pathlib import Path

import pytest

SOURCE_CAPABILITY_TOOLS = {"spawn_subagent", "propose_dynamic_workflow", "preview_workflow", "start_workflow"}
WORK_LEDGER_TOOLS = {"track_todo", "record_finding", "read_ledger"}
COMMAND_PARITY_TOOLS = {
    "task_create",
    "task_update",
    "task_list",
    "task_get",
    "task_output",
    "task_stop",
    "goal_start",
    "advanced_plan",
    "verify_plan",
}
ADVANCED_WEB_TOOLS = {
    "advanced_web_search",
    "advanced_web_fetch",
    "anysearch_get_sub_domains",
    "anysearch_search",
    "anysearch_batch_search",
    "anysearch_extract",
    "exa_search",
    "exa_fetch",
    "tavily_search",
    "tavily_extract",
    "firecrawl_search",
    "firecrawl_fetch",
    "xcrawl_scrape",
}
CODING_PLUGIN_TOOLS = {"lsp_symbol_search", "worktree_create", "notebook_edit", "browser_ui_open"}
OFFICE_RUNTIME_TOOLS = {
    "read_document",
    "office_document_create",
    "office_document_view",
    "office_document_query",
    "office_document_apply",
    "office_document_validate",
    "office_document_dump",
}


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


def test_core_tool_names_include_command_parity_wrappers():
    """CC/Codex command wrappers are runtime primitives, not disableable pack add-ons."""
    from app.services.agent_tools import CORE_TOOL_NAMES

    assert COMMAND_PARITY_TOOLS <= CORE_TOOL_NAMES


def test_collected_surface_provides_schemas_for_core_command_parity_tools():
    from app.services.agent_tools import CORE_TOOL_NAMES, get_combined_openai_tools

    combined_names = {t["function"]["name"] for t in get_combined_openai_tools()}
    assert COMMAND_PARITY_TOOLS <= (combined_names & CORE_TOOL_NAMES)


def test_session_and_extension_surfaces_use_taxonomy_facade_instead_of_runtime_groups():
    import app.api.agents as agents_api
    import app.runtime.invoker as invoker

    assert "RUNTIME_TOOL_GROUPS" not in inspect.getsource(invoker._infer_active_tool_groups)
    assert "RUNTIME_TOOL_GROUPS" not in inspect.getsource(agents_api.get_agent_extension_registry)


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


def test_runtime_tool_groups_are_compat_projection_of_taxonomy():
    import app.services.governance_capability_taxonomy as taxonomy
    from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS

    taxonomy_source = inspect.getsource(taxonomy)
    assert "app.tools.runtime_tool_groups" not in taxonomy_source
    assert "RUNTIME_TOOL_GROUPS" not in taxonomy_source

    l2_by_name = {descriptor.name: descriptor for descriptor in taxonomy.iter_runtime_l2_capabilities()}
    runtime_by_name = {group.name: group for group in RUNTIME_TOOL_GROUPS}

    assert runtime_by_name.keys() == l2_by_name.keys()
    for name, descriptor in l2_by_name.items():
        group = runtime_by_name[name]
        assert group.tools == descriptor.tools
        assert group.source == descriptor.source
        assert group.summary == descriptor.notes


def test_l2_taxonomy_decorator_and_pack_manifests_are_consistent():
    from app.packs.catalog_reader import PackCatalogReader, find_pack_dirs
    from app.services.governance_capability_taxonomy import (
        CAPABILITY_MAP,
        CORE_TOOL_NAMES,
        RUNTIME_L2_CAPABILITY_SPECS,
    )
    from app.tools.collector import collect_tools

    taxonomy = {
        spec.name: tools
        for spec in RUNTIME_L2_CAPABILITY_SPECS
        if (tools := {tool for tool in spec.tools if tool not in CORE_TOOL_NAMES})
    }
    decorator_groups = {
        name: set(tools) for name, tools in collect_tools().pack_tool_groups.items() if name in taxonomy
    }
    manifest_groups: dict[str, set[str]] = {}
    for packs_dir in find_pack_dirs(Path(__file__).resolve()):
        reader = PackCatalogReader(packs_dir)
        reader.discover()
        for manifest in reader.list_packs():
            if manifest.name not in taxonomy:
                continue
            manifest_tools = set(manifest.owns_names) | set(manifest.optional_provider_names)
            if manifest_tools:
                manifest_groups[manifest.name] = manifest_tools
            assert set(manifest.requires_core_names) <= CORE_TOOL_NAMES

    assert decorator_groups == taxonomy
    assert manifest_groups == taxonomy
    assert set().union(*taxonomy.values()) <= set(CAPABILITY_MAP)


def test_coding_capability_is_l2_local_bridge_only():
    from app.services.coding_pack_manifest import CODING_PACK_TOOLS
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
    assert tuple(descriptor.tools) == CODING_PACK_TOOLS
    assert CODING_PLUGIN_TOOLS <= set(descriptor.tools)


def test_office_runtime_is_core_and_browser_office_is_not_cloud_descriptor():
    from app.services.agent_tools import CORE_TOOL_NAMES
    from app.services.governance_capability_taxonomy import (
        GovernanceCapabilityLayer,
        capability_descriptor_for_name,
        capability_descriptor_for_tool,
        iter_l2_capabilities,
    )

    assert OFFICE_RUNTIME_TOOLS <= CORE_TOOL_NAMES
    for tool_name in OFFICE_RUNTIME_TOOLS:
        descriptor = capability_descriptor_for_tool(tool_name)
        assert descriptor is not None
        assert descriptor.layer == GovernanceCapabilityLayer.AGENT_BASE.value
        assert descriptor.l2_visible is False

    l2_tools = {tool for descriptor in iter_l2_capabilities() for tool in descriptor.tools}
    assert not (OFFICE_RUNTIME_TOOLS & l2_tools)

    browser_office = capability_descriptor_for_name("office_browser")
    assert browser_office is None
    assert capability_descriptor_for_tool("onlyoffice_browser_session") is None
    assert capability_descriptor_for_tool("onlyoffice_signed_callback") is None


@pytest.mark.asyncio
async def test_explicit_agent_team_discovers_team_create_without_command_pack(monkeypatch):
    """Agent Team creation follows CC-style deferred discovery.

    `team_create` must be name-visible/selectable even when the historical
    command_pack is inactive; otherwise an explicit Agent Team request silently
    degrades to plain `spawn_subagent`.
    """
    from uuid import uuid4

    import app.services.agent_tools as agent_tools

    class _TenantSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    async def fake_resolve_tenant(_agent_id):
        return uuid4()

    async def fake_get_agent_capability_group_policies(_db, _tenant_id, _agent_id):
        return {}

    async def fake_mcp(_agent_id, _query):
        return []

    monkeypatch.setattr(agent_tools, "resolve_tenant_for_agent", fake_resolve_tenant)
    monkeypatch.setattr(agent_tools, "tenant_scoped_session", lambda _tenant_id: _TenantSession())
    monkeypatch.setattr(agent_tools, "get_agent_capability_group_policies", fake_get_agent_capability_group_policies)
    monkeypatch.setattr(agent_tools, "list_agent_mcp_deferred_tools", fake_mcp)

    assert "team_create" in await agent_tools.available_deferred_tool_names_for_agent(uuid4())
    assert await agent_tools.discoverable_tool_names_for_query(uuid4(), "select:team_create") == ["team_create"]


def test_command_pack_no_longer_owns_agent_team_runtime_discovery():
    """The historical command_pack may remain a slash-command facade, but it
    must not own the model-visible Agent Team creation path."""
    from app.services.governance_capability_taxonomy import iter_runtime_l2_capabilities
    from app.services.capability_group_policy_service import policy_capability_group_names_for_tool
    from app.tools.collector import collect_tools

    command_pack = next(
        (descriptor for descriptor in iter_runtime_l2_capabilities() if descriptor.name == "command_pack"), None
    )

    assert command_pack is None or "team_create" not in command_pack.tools
    assert "team_create" not in collect_tools().pack_tool_groups.get("command_pack", [])
    assert policy_capability_group_names_for_tool("team_create") == ()


def test_command_pack_task_wrappers_do_not_duplicate_work_ledger_runtime_surface():
    from app.services.agent_tools import CORE_TOOL_NAMES
    from app.services.governance_capability_taxonomy import capability_descriptor_for_tool, iter_runtime_l2_capabilities
    from app.services.capability_group_policy_service import policy_capability_group_names_for_tool
    from app.tools.collector import collect_tools

    command_pack = next(
        (descriptor for descriptor in iter_runtime_l2_capabilities() if descriptor.name == "command_pack"), None
    )

    assert {"track_todo", "record_finding", "read_ledger"} <= CORE_TOOL_NAMES
    assert COMMAND_PARITY_TOOLS <= CORE_TOOL_NAMES
    assert command_pack is None or not (COMMAND_PARITY_TOOLS & set(command_pack.tools))
    assert not (COMMAND_PARITY_TOOLS & set(collect_tools().pack_tool_groups.get("command_pack", [])))
    for tool_name in COMMAND_PARITY_TOOLS:
        descriptor = capability_descriptor_for_tool(tool_name)
        assert descriptor is not None
        assert descriptor.name == "agent_base"
        assert policy_capability_group_names_for_tool(tool_name) == ()


def test_all_pack_manifests_root_backend_consistent():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    backend_packs = repo / "backend" / "packs"
    root_packs = repo / "packs"

    backend_manifests = {
        path.parent.name: path.read_text(encoding="utf-8") for path in backend_packs.glob("*/pack.yaml")
    }
    root_manifests = {path.parent.name: path.read_text(encoding="utf-8") for path in root_packs.glob("*/pack.yaml")}

    assert root_manifests.keys() == backend_manifests.keys()
    assert root_manifests == backend_manifests


@pytest.mark.asyncio
async def test_discoverable_l2_tools_fail_closed_when_pack_policy_unavailable(monkeypatch):
    from uuid import uuid4

    import app.services.agent_tools as agent_tools

    async def fail_resolve(_agent_id):
        raise RuntimeError("tenant unavailable")

    async def no_mcp(_agent_id, _query):
        return []

    monkeypatch.setattr(agent_tools, "resolve_tenant_for_agent", fail_resolve)
    monkeypatch.setattr(agent_tools, "list_agent_mcp_deferred_tools", no_mcp)

    assert await agent_tools.discoverable_tool_names_for_query(uuid4(), "exa") == []
