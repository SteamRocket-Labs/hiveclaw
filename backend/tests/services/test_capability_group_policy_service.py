from __future__ import annotations


def test_capability_group_policy_facade_preserves_legacy_pack_storage_names():
    from app.services.capability_group_policy_service import (
        is_capability_group_enabled,
        policy_capability_group_names_for_tool,
    )
    from app.services.governance_capability_taxonomy import taxonomy_policy_capability_group_names_for_tool

    assert policy_capability_group_names_for_tool("exa_search") == taxonomy_policy_capability_group_names_for_tool(
        "exa_search"
    )
    assert "web_pack" in policy_capability_group_names_for_tool("exa_search")
    assert is_capability_group_enabled({"web_pack": False}, "web_pack") is False
    assert is_capability_group_enabled({"web_pack": True}, "web_pack") is True


def test_runtime_surfaces_import_capability_group_policy_facade():
    from app.services import agent_tools
    from app.tools import service as tool_service

    assert agent_tools.get_agent_capability_group_policies is not None
    assert agent_tools.is_capability_group_enabled is not None
    assert agent_tools.policy_capability_group_names_for_tool is not None
    assert tool_service.get_agent_capability_group_policies is not None
    assert tool_service.is_capability_group_enabled is not None
    assert tool_service.policy_capability_group_names_for_tool is not None
    assert not hasattr(agent_tools, "get_agent_pack_policies")
    assert not hasattr(tool_service, "policy_pack_names_for_tool")
