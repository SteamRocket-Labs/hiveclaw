from __future__ import annotations

import pytest


def test_dynamic_command_loader_registers_derived_skill_workflow_or_mcp_command():
    from app.services.command_registry import build_default_command_registry

    registry = build_default_command_registry(
        dynamic_commands=[
            {
                "name": "skill_memory_audit",
                "description": "Load the memory audit skill capsule.",
                "category": "skill",
                "source": "skill",
                "execution_mode": "tool",
                "handler_ref": "skill:memory_audit",
                "input_schema": {"type": "object", "properties": {"depth": {"type": "string"}}},
            }
        ]
    )

    command = registry.get("skill_memory_audit")

    assert command.source == "skill"
    assert command.handler_ref == "skill:memory_audit"
    assert command.input_schema["properties"]["depth"]["type"] == "string"


def test_optional_coding_pack_is_registered_but_not_user_or_prompt_default():
    from app.services.command_registry import build_default_command_registry

    with pytest.raises(KeyError):
        build_default_command_registry().get("diff")

    registry = build_default_command_registry(include_optional_coding_pack=True)
    prompt_names = {entry["name"] for entry in registry.visible_index(surface="agent_prompt")}
    user_names = {entry["name"] for entry in registry.visible_index(surface="user")}

    assert "diff" not in prompt_names
    assert "diff" not in user_names
    assert registry.get("diff").execution_mode == "external"
    assert registry.get("shell_pack").execution_mode == "external"
    assert registry.get("shell_pack").bridge_safe is False


def test_optional_coding_pack_can_be_model_visible_after_policy_activation():
    from app.services.command_registry import build_default_command_registry

    registry = build_default_command_registry(
        include_optional_coding_pack=True,
        optional_coding_pack_model_visible=True,
    )
    prompt_names = {entry["name"] for entry in registry.visible_index(surface="agent_prompt")}

    assert "diff" in prompt_names
    assert registry.get("diff").visible_to_model is True
