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


def test_subagent_prompt_guidance_names_cc_trigger_scenarios() -> None:
    from app.runtime.prompt_sections.executing_actions import build_executing_actions_section

    section = build_executing_actions_section()

    assert "Fan out independent read-only searches" in section
    assert "isolate a noisy exploration" in section
    assert "After non-trivial code changes" in section
    assert "critic" in section
