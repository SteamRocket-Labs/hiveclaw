from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_feishu_url_resolve_unwraps_wiki_file_node(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_drive

    async def fake_get_feishu_token(_agent_id: uuid.UUID | str) -> tuple[str, str]:
        return ("app", "tenant-token")

    async def fake_wiki_get_node(node_token: str, tenant_access_token: str) -> dict:
        assert node_token == "wiki-file-node"
        assert tenant_access_token == "tenant-token"
        return {
            "node_token": "wiki-file-node",
            "title": "BP deck",
            "obj_type": "file",
            "obj_token": "file-token",
        }

    monkeypatch.setattr(feishu_drive, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_drive, "_feishu_wiki_get_node", fake_wiki_get_node)

    result = await feishu_drive._feishu_url_resolve(
        "agent-1",
        {"url": "https://example.feishu.cn/wiki/wiki-file-node"},
    )

    assert "obj_type: `file`" in result
    assert "obj_token: `file-token`" in result
    assert 'feishu_drive_file_read(file_token="file-token"' in result


@pytest.mark.asyncio
async def test_feishu_url_read_routes_docx_url_to_doc_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_docs, feishu_drive

    async def fake_doc_read(agent_id: uuid.UUID | str, arguments: dict) -> str:
        assert agent_id == "agent-1"
        assert arguments == {"document_token": "doc-token", "max_chars": 1200}
        return "DOC CONTENT"

    monkeypatch.setattr(feishu_docs, "_feishu_doc_read", fake_doc_read)

    result = await feishu_drive._feishu_url_read(
        "agent-1",
        {"url": "https://example.feishu.cn/docx/doc-token?from=table", "max_chars": 1200},
    )

    assert result == "DOC CONTENT"


@pytest.mark.asyncio
async def test_feishu_url_read_routes_base_url_to_records_when_table_id_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_base, feishu_drive

    async def fake_record_list(agent_id: uuid.UUID | str, arguments: dict) -> str:
        assert agent_id == "agent-1"
        assert arguments == {
            "base_token": "base-token",
            "table_id": "tbl_1",
            "view_id": "viw_1",
            "limit": 50,
            "max_chars": 2000,
            "fetch_all": True,
            "max_records": 1000,
            "field_names": ["项目名称", "净利润"],
            "filter_field": "净利润",
            "filter_op": "<",
            "filter_value": "0",
        }
        return "BASE RECORDS WITH URL FIELDS"

    monkeypatch.setattr(feishu_base, "_feishu_base_record_list", fake_record_list)

    result = await feishu_drive._feishu_url_read(
        "agent-1",
        {
            "url": "https://example.feishu.cn/base/base-token?table=tbl_1",
            "table_id": "tbl_1",
            "view_id": "viw_1",
            "limit": 50,
            "max_chars": 2000,
            "fetch_all": True,
            "max_records": 1000,
            "field_names": ["项目名称", "净利润"],
            "filter_field": "净利润",
            "filter_op": "<",
            "filter_value": "0",
        },
    )

    assert result == "BASE RECORDS WITH URL FIELDS"


@pytest.mark.asyncio
async def test_feishu_url_read_routes_wiki_file_node_to_drive_file_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_drive

    async def fake_get_feishu_token(_agent_id: uuid.UUID | str) -> tuple[str, str]:
        return ("app", "tenant-token")

    async def fake_wiki_get_node(node_token: str, tenant_access_token: str) -> dict:
        assert node_token == "wiki-file-node"
        assert tenant_access_token == "tenant-token"
        return {
            "node_token": "wiki-file-node",
            "title": "需求说明.xlsx",
            "obj_type": "file",
            "obj_token": "file-token",
        }

    async def fake_drive_file_read(agent_id: uuid.UUID | str, arguments: dict) -> str:
        assert agent_id == "agent-1"
        assert arguments == {
            "file_token": "file-token",
            "file_name": "需求说明.xlsx",
            "max_chars": 3000,
        }
        return "EXTRACTED FILE TEXT"

    monkeypatch.setattr(feishu_drive, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_drive, "_feishu_wiki_get_node", fake_wiki_get_node)
    monkeypatch.setattr(feishu_drive, "_feishu_drive_file_read", fake_drive_file_read)

    result = await feishu_drive._feishu_url_read(
        "agent-1",
        {"url": "https://example.feishu.cn/wiki/wiki-file-node", "max_chars": 3000},
    )

    assert result == "EXTRACTED FILE TEXT"


@pytest.mark.asyncio
async def test_feishu_drive_file_read_downloads_file_and_extracts_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_drive

    async def fake_get_feishu_token(_agent_id: uuid.UUID | str) -> tuple[str, str]:
        return ("app", "tenant-token")

    async def fake_download_drive_file(
        file_token: str,
        tenant_access_token: str,
        *,
        file_name: str | None = None,
    ) -> tuple[bytes, str, dict]:
        assert file_token == "file-token"
        assert tenant_access_token == "tenant-token"
        assert file_name == "deck.pptx"
        return (b"pptx-bytes", "deck.pptx", {"source": "drive_download"})

    def fake_extract_text(content: bytes, filename: str) -> str:
        assert content == b"pptx-bytes"
        assert filename == "deck.pptx"
        return "Slide 1\nBusiness plan"

    monkeypatch.setattr(feishu_drive, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_drive, "_download_drive_file", fake_download_drive_file)
    monkeypatch.setattr(feishu_drive, "extract_text", fake_extract_text)

    result = await feishu_drive._feishu_drive_file_read(
        "agent-1",
        {"file_token": "file-token", "file_name": "deck.pptx", "max_chars": 100},
    )

    assert "deck.pptx" in result
    assert "Slide 1" in result
    assert "Business plan" in result
    assert result.metadata["connector_source_items"] == [
        {
            "source": "feishu://drive/file-token",
            "acl": {"agent_ids": ["agent-1"]},
            "metadata": {"connector": "feishu", "resource_type": "drive_file"},
        }
    ]


@pytest.mark.asyncio
async def test_feishu_drive_file_read_exports_online_sheet_and_extracts_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_drive

    async def fake_get_feishu_token(_agent_id: uuid.UUID | str) -> tuple[str, str]:
        return ("app", "tenant-token")

    async def fake_export_online_document(
        document_token: str,
        document_type: str,
        file_extension: str,
        tenant_access_token: str,
        *,
        sub_id: str | None = None,
    ) -> tuple[bytes, str, dict]:
        assert document_token == "sheet-token"
        assert document_type == "sheet"
        assert file_extension == "xlsx"
        assert tenant_access_token == "tenant-token"
        assert sub_id is None
        return (b"xlsx-bytes", "sheet-token.xlsx", {"source": "export_task", "job_status": 0})

    def fake_extract_text(content: bytes, filename: str) -> str:
        assert content == b"xlsx-bytes"
        assert filename == "sheet-token.xlsx"
        return "A1\tA2\n客户\t金额"

    monkeypatch.setattr(feishu_drive, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_drive, "_export_online_document", fake_export_online_document)
    monkeypatch.setattr(feishu_drive, "extract_text", fake_extract_text)

    result = await feishu_drive._feishu_drive_file_read(
        "agent-1",
        {"token": "sheet-token", "type": "sheet", "file_extension": "xlsx", "max_chars": 100},
    )

    assert "sheet-token.xlsx" in result
    assert "客户" in result
    assert result.metadata["connector_source_items"] == [
        {
            "source": "feishu://drive/sheet-token",
            "acl": {"agent_ids": ["agent-1"]},
            "metadata": {"connector": "feishu", "resource_type": "drive_file"},
        }
    ]
