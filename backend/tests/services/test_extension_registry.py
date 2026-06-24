from __future__ import annotations

from pathlib import Path

from app.runtime.hooks import HookEvent
from app.skills.types import ParsedSkill, SkillMetadata


def _skill(name: str, **kwargs) -> ParsedSkill:
    return ParsedSkill(
        metadata=SkillMetadata(name=name, description=f"{name} desc", **kwargs),
        body="# " + name,
        file_path=Path(f"skills/{name}/SKILL.md"),
        relative_path=f"skills/{name}/SKILL.md",
    )


def test_extension_registry_projection_unifies_skill_hook_command_and_mcp_discovery_surfaces():
    from app.services.command_registry import _command
    from app.services.extension_registry import build_extension_registry_projection

    registry = build_extension_registry_projection(
        skills=[
            _skill("research", declared_tools=("web_search",), hidden=True),
        ],
        hook_catalog=[
            {
                "event": HookEvent.POST_TOOL_USE.value,
                "runtime_consumer": "kernel_post_tool_rewrite_consumer",
                "lifecycle_state": "active_observe",
            }
        ],
        commands=[
            _command(
                "mcp_prompt_review",
                "Review via MCP prompt.",
                category="mcp",
                source="mcp",
                execution_mode="external",
                handler_ref="mcp:server:prompts/review",
                bridge_safe=False,
            )
        ],
        mcp_servers=[
            {
                "id": "srv-1",
                "name": "docs",
                "tools": ["mcp__docs__search"],
                "prompts": ["review"],
                "resources": ["skill://docs/research"],
                "audit_refs": ["audit://mcp/srv-1"],
            }
        ],
    )

    by_id = {extension.id: extension for extension in registry.extensions}

    assert by_id["skill:research"].type == "skill"
    assert by_id["skill:research"].exposed_tools == ("web_search",)
    assert "hidden_from_model_catalog" in by_id["skill:research"].runtime_effects
    assert by_id["hook:post_tool_use"].runtime_effects == (
        "active_observe",
        "consumer:kernel_post_tool_rewrite_consumer",
    )
    assert by_id["command:mcp_prompt_review"].source == "mcp"
    assert by_id["mcp_server:srv-1"].exposed_tools == ("mcp__docs__search",)
    assert "mcp_prompt:review->command" in by_id["mcp_server:srv-1"].runtime_effects
    assert "mcp_resource:skill://docs/research->skill" in by_id["mcp_server:srv-1"].runtime_effects
    assert by_id["mcp_server:srv-1"].audit_refs == ("audit://mcp/srv-1",)
