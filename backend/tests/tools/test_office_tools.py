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


def test_office_tools_are_registered_with_pack_and_capability_surface():
    from app.services.capability_gate import CAPABILITY_MAP
    from app.tools.collector import collect_tools

    collected = collect_tools()
    registered = {tool["function"]["name"] for tool in collected.openai_tools}

    assert OFFICE_TOOL_NAMES.issubset(registered)
    assert set(collected.pack_tool_groups["office_pack"]).issuperset(OFFICE_TOOL_NAMES)
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


@pytest.mark.asyncio
async def test_office_document_apply_tool_rejects_active_editor_session(tmp_path):
    from app.services.office_document_service import OfficeDocumentService
    from app.tools.handlers.office import office_document_apply

    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-version")
    OfficeDocumentService(tmp_path).set_active_editor_session(
        "workspace/demo.docx",
        session_id="session-1",
        user_id="user-1",
    )

    result = json.loads(
        await office_document_apply(
            tmp_path,
            {
                "path": "workspace/demo.docx",
                "operations": [{"op": "replace_text", "from": "old", "to": "new"}],
            },
        )
    )

    assert result["ok"] is False
    assert result["error"] == "active_editor_session"
    assert target.read_bytes() == b"old-version"
