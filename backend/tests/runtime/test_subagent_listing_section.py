"""Prompt-facing session-worker type listing."""

from __future__ import annotations

from pathlib import Path


def test_subagent_listing_section_renders_builtin_types_and_when_to_use() -> None:
    from app.runtime.prompt_sections.subagent_listing import build_subagent_listing_section

    section = build_subagent_listing_section()

    assert "## Session Worker Types" in section
    assert "spawn_subagent" in section
    for name in ("general-purpose", "explorer", "critic"):
        assert f"`{name}`" in section
    assert "`worker`" not in section

    assert "Default general-purpose session-local worker" in section
    assert "Fast read-only agent" in section
    assert "verify that work is correct" in section
    assert "not A2A employees" in section


def test_subagent_listing_section_teaches_agent_team_deferred_create_path() -> None:
    from app.runtime.prompt_sections.subagent_listing import build_subagent_listing_section

    section = build_subagent_listing_section()

    assert "## Agent Team vs Session Workers" in section
    assert "explicitly asks for Agent Team" in section
    assert "tool_search" in section
    assert "select:team_create" in section
    assert "team_create" in section
    assert "spawn_subagent" in section
    assert "team_name" in section
    assert "name" in section
    assert "Do not silently downgrade" in section
    assert "Dynamic Workflow" in section


def test_subagent_listing_section_includes_custom_definitions_in_same_spawn_path(tmp_path: Path) -> None:
    from app.agents.subagent import SubagentSpec
    from app.agents.subagent_definition import definition_store_for_agent, definition_store_for_tenant
    from app.runtime.prompt_sections.subagent_listing import build_subagent_listing_section

    agent_id = "agent-1"
    tenant_id = "tenant-1"
    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(
            name="code-critic",
            description="Use after non-trivial code changes to verify tests and risks.",
            type="critic",
            allowed_tools=("read_file", "grep_search"),
            max_tool_rounds=4,
            system_prompt="Verify the implementation.",
        )
    )
    definition_store_for_tenant(tenant_id, agent_data_dir=tmp_path).save(
        SubagentSpec(
            name="market-scout",
            description="Use for broad market research that would pollute the parent context.",
            type="explorer",
            allowed_tools=("web_search",),
            system_prompt="Explore the market.",
        )
    )

    section = build_subagent_listing_section(agent_id=agent_id, tenant_id=tenant_id, agent_data_dir=tmp_path)

    assert "### Custom Session Worker Definitions" in section
    assert "setting `definition_name`" in section
    assert "`code-critic` (agent, type=`critic`)" in section
    assert "`market-scout` (tenant, type=`explorer`)" in section
    assert "same `spawn_subagent` tool" in section


def test_subagent_listing_preserves_complete_allowed_tool_surface(tmp_path: Path) -> None:
    from app.agents.subagent import SubagentSpec
    from app.agents.subagent_definition import definition_store_for_agent
    from app.runtime.prompt_sections.subagent_listing import build_subagent_listing_section

    agent_id = "agent-full-tools"
    allowed_tools = tuple(f"tool_{index}" for index in range(8))
    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(
            name="full-tool-worker",
            description="Worker with a complete governed tool surface.",
            type="general-purpose",
            allowed_tools=allowed_tools,
            system_prompt="Use every authorized tool when relevant.",
        )
    )

    section = build_subagent_listing_section(agent_id=agent_id, agent_data_dir=tmp_path)

    assert "tool_7" in section


def test_subagent_listing_projects_activation_keys_for_builtin_and_custom_definitions(tmp_path: Path) -> None:
    from app.agents.subagent import SubagentSpec
    from app.agents.subagent_definition import definition_store_for_agent
    from app.runtime.prompt_sections.subagent_listing import build_subagent_listing_section

    agent_id = "agent-keys"
    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(
            name="code-critic",
            description="Use after non-trivial code changes to verify tests and risks.",
            type="critic",
            allowed_tools=("read_file", "grep_search"),
            max_tool_rounds=4,
            system_prompt="Verify implementation.",
        )
    )

    manifest: list[dict] = []
    build_subagent_listing_section(agent_id=agent_id, agent_data_dir=tmp_path, activation_key_manifest=manifest)

    by_name = {entry["key_features"]["name"][0]: entry for entry in manifest}
    builtin = by_name["critic"]
    custom = by_name["code-critic"]

    assert builtin["schema_version"] == "runtime.metadata_activation_keys.20260705"
    assert builtin["candidate_kind"] == "subagent"
    assert builtin["candidate_ref"]["source_type"] == "subagent_builtin"
    assert builtin["value_pointer"]["loader"] == "spawn_subagent"
    assert builtin["value_pointer"]["subagent_type"] == "critic"
    assert custom["candidate_ref"]["source_type"] == "subagent_definition"
    assert custom["key_features"]["scope"] == ["agent"]
    assert custom["key_features"]["type"] == ["critic"]
    assert "read_file" in custom["key_features"]["allowed_tools"]
    assert custom["value_pointer"]["definition_name"] == "code-critic"


def test_subagent_gatherer_outputs_activation_candidates_for_builtin_and_custom(tmp_path: Path) -> None:
    from app.agents.subagent import SubagentSpec
    from app.agents.subagent_definition import definition_store_for_agent
    from app.runtime.prompt_sections.subagent_listing import gather_subagent_candidates

    agent_id = "agent-candidates"
    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(
            name="code-critic",
            description="Use after non-trivial code changes to verify tests and risks.",
            type="critic",
            allowed_tools=("read_file", "grep_search"),
            max_tool_rounds=4,
            system_prompt="Verify implementation.",
        )
    )

    candidates = gather_subagent_candidates(agent_id=agent_id, agent_data_dir=tmp_path)
    manifests = {
        candidate.to_manifest()["key_features"]["name"][0]: candidate.to_manifest() for candidate in candidates
    }

    builtin = manifests["critic"]
    custom = manifests["code-critic"]

    assert builtin["candidate_kind"] == "subagent"
    assert builtin["candidate_ref"]["source_type"] == "subagent_builtin"
    assert builtin["value_pointer"]["loader"] == "spawn_subagent"
    assert builtin["value_pointer"]["subagent_type"] == "critic"
    assert builtin["surface"]["surface_kind"] == "subagent_listing"
    assert custom["candidate_ref"]["source_type"] == "subagent_definition"
    assert custom["value_pointer"]["definition_name"] == "code-critic"
    assert custom["source_refs"] == ["subagent:agent:code-critic"]


def test_subagent_prompt_guidance_names_cc_trigger_scenarios() -> None:
    from app.runtime.prompt_sections.executing_actions import build_executing_actions_section

    section = build_executing_actions_section()

    assert "Fan out independent read-only searches" in section
    assert "isolate a noisy exploration" in section
    assert "After non-trivial code changes" in section
    assert "critic" in section
