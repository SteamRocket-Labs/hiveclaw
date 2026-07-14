from __future__ import annotations

import json

import pytest


OFFICE_TOOL_NAMES = {
    "office_document_create",
    "office_document_view",
    "office_document_query",
    "office_document_apply",
    "office_document_validate",
    "office_document_dump",
}


def test_office_tools_are_registered_as_agent_base_capability_surface():
    from app.services.capability_gate import CAPABILITY_MAP
    from app.services.governance_capability_taxonomy import GovernanceCapabilityLayer, capability_descriptor_for_tool
    from app.tools.collector import collect_tools

    collected = collect_tools()
    registered = {tool["function"]["name"] for tool in collected.openai_tools}
    office_pack_tools = set(collected.pack_tool_groups.get("office_pack", []))

    assert OFFICE_TOOL_NAMES.issubset(registered)
    assert not (OFFICE_TOOL_NAMES & office_pack_tools)
    for tool_name in OFFICE_TOOL_NAMES:
        descriptor = capability_descriptor_for_tool(tool_name)
        assert descriptor is not None
        assert descriptor.layer == GovernanceCapabilityLayer.AGENT_BASE.value
    assert CAPABILITY_MAP["office_document_create"] == "workspace.file.write"
    assert CAPABILITY_MAP["office_document_apply"] == "workspace.file.write"
    assert CAPABILITY_MAP["office_document_view"] == "workspace.file.read"
    assert CAPABILITY_MAP["office_document_query"] == "workspace.file.read"
    assert CAPABILITY_MAP["office_document_validate"] == "workspace.file.read"
    assert CAPABILITY_MAP["office_document_dump"] == "workspace.file.read"


@pytest.mark.asyncio
async def test_office_document_create_tool_creates_docx(tmp_path):
    from app.tools.handlers.office import office_document_create

    result = json.loads(
        await office_document_create(
            tmp_path,
            {"path": "workspace/demo.docx", "kind": "docx"},
            tenant_id="tenant-1",
        )
    )

    assert result["ok"] is True
    assert result["path"] == "workspace/demo.docx"
    assert result["connector_source_items"] == [
        {
            "source": "office://workspace/workspace/demo.docx",
            "acl": {"tenant_ids": ["tenant-1"], "scope": "tenant"},
            "metadata": {"connector": "office", "resource_type": "document", "acl_authority": "connector_verified"},
        }
    ]
    assert (tmp_path / "workspace" / "demo.docx").is_file()


def test_office_document_apply_tool_has_no_retired_editor_session_bypass():
    from app.tools.collector import collect_tools

    collected = collect_tools()
    definition = next(
        tool["function"]
        for tool in collected.openai_tools
        if tool["function"]["name"] == "office_document_apply"
    )

    assert "require_no_active_editor" not in definition["parameters"]["properties"]
    assert "editor session" not in definition["description"].lower()
