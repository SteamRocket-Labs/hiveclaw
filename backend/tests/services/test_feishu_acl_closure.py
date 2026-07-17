from __future__ import annotations

import uuid

import pytest


def _assert_verified_sources(result, expected_sources: set[str]) -> None:
    from app.services.connector_acl import extract_connector_source_items

    source_items = extract_connector_source_items(result)
    assert {item["source"] for item in source_items} == expected_sources
    assert all(item["metadata"]["acl_authority"] == "connector_verified" for item in source_items)
    assert all(item["acl"] == {"tenant_ids": ["tenant-1"], "user_ids": ["user-1"]} for item in source_items)


def test_feishu_argument_sources_cover_reads_without_treating_writes_as_read_evidence() -> None:
    from app.services.connector_acl import source_items_from_tool_call

    assert [
        item["source"] for item in source_items_from_tool_call("feishu_wiki_list", {"node_token": "wiki-node"})
    ] == ["feishu://wiki/node/wiki-node"]
    assert [item["source"] for item in source_items_from_tool_call("feishu_wiki_list", {"space_id": "space-1"})] == [
        "feishu://wiki/space/space-1"
    ]
    assert [
        item["source"] for item in source_items_from_tool_call("feishu_base_table_list", {"base_token": "base-1"})
    ] == ["feishu://base/base-1"]
    assert {
        item["source"]
        for item in source_items_from_tool_call(
            "feishu_base_record_list",
            {"base_token": "base-1", "table_id": "table-1"},
        )
    } == {"feishu://base/base-1", "feishu://base/base-1/table-1"}
    assert (
        source_items_from_tool_call(
            "feishu_base_record_upsert",
            {"base_token": "base-1", "table_id": "table-1", "fields": {"name": "value"}},
        )
        == []
    )


def test_feishu_argument_sources_normalize_supported_full_urls_to_connector_tokens() -> None:
    from app.services.connector_acl import source_items_from_tool_call

    assert [
        item["source"]
        for item in source_items_from_tool_call(
            "feishu_doc_read",
            {"document_token": "https://example.feishu.cn/wiki/wiki-node?from=chat"},
        )
    ] == ["feishu://doc/wiki-node"]
    assert [
        item["source"]
        for item in source_items_from_tool_call(
            "feishu_drive_file_read",
            {"file_token": "https://example.feishu.cn/file/file-token"},
        )
    ] == ["feishu://drive/file-token"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "service_name", "arguments"),
    [
        ("feishu_wiki_list", "_feishu_wiki_list", {"node_token": "wiki-node"}),
        ("feishu_drive_file_read", "_feishu_drive_file_read", {"file_token": "file-token"}),
        ("feishu_sheet_info", "_feishu_sheet_info", {"spreadsheet_token": "sheet-token"}),
        (
            "feishu_sheet_read",
            "_feishu_sheet_read",
            {"spreadsheet_token": "sheet-token", "range": "A1:B2"},
        ),
        ("feishu_base_table_list", "_feishu_base_table_list", {"base_token": "base-token"}),
        (
            "feishu_base_record_list",
            "_feishu_base_record_list",
            {"base_token": "base-token", "table_id": "table-token"},
        ),
        (
            "feishu_base_field_list",
            "_feishu_base_field_list",
            {"base_token": "base-token", "table_id": "table-token"},
        ),
    ],
)
async def test_feishu_read_handlers_forward_authenticated_acl_subject(
    monkeypatch: pytest.MonkeyPatch,
    handler_name: str,
    service_name: str,
    arguments: dict,
) -> None:
    from pathlib import Path

    from app.services import agent_tools
    from app.tools.adapters import adapt_and_call
    from app.tools.handlers import feishu as feishu_handler
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def allow(_agent_id: uuid.UUID) -> bool:
        return True

    async def fake_service(
        received_agent_id: uuid.UUID,
        received_arguments: dict,
        *,
        tenant_id: uuid.UUID | str | None = None,
        current_user_id: uuid.UUID | str | None = None,
    ) -> str:
        assert received_agent_id == agent_id
        assert received_arguments == arguments
        assert tenant_id == str(expected_tenant_id)
        assert current_user_id == user_id
        return "authorized Feishu result"

    expected_tenant_id = tenant_id
    monkeypatch.setattr(feishu_handler, "_check_feishu_office_access", allow)
    monkeypatch.setattr(feishu_handler, "_check_feishu_cli_access", allow)
    monkeypatch.setattr(agent_tools, service_name, fake_service)

    handler = getattr(feishu_handler, handler_name)
    request = ToolExecutionRequest(
        tool_name=handler_name,
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=str(tenant_id),
            workspace=Path("."),
        ),
    )

    result = await adapt_and_call(handler.meta, handler, request)

    assert result == "authorized Feishu result"


@pytest.mark.asyncio
async def test_feishu_sheet_success_upgrades_argument_source_for_authenticated_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_sheets
    from app.services.connector_acl import (
        CONNECTOR_SOURCE_ITEMS_METADATA_KEY,
        register_connector_source_items,
        register_connector_source_payload,
        source_items_from_tool_call,
    )

    async def cli_available() -> bool:
        return True

    async def run_shortcut(_args: list[str]) -> dict:
        return {
            "spreadsheet": {"spreadsheet_token": "sheet-token", "title": "Budget"},
            "sheets": [{"sheet_id": "sheet-1", "title": "Overview", "grid_properties": {}}],
        }

    monkeypatch.setattr(feishu_sheets, "_feishu_cli_available", cli_available)
    monkeypatch.setattr(feishu_sheets, "_run_feishu_sheet_shortcut", run_shortcut)

    result = await feishu_sheets._feishu_sheet_info(
        "agent-1",
        {"spreadsheet_token": "sheet-token"},
        tenant_id="tenant-1",
        current_user_id="user-1",
    )

    _assert_verified_sources(result, {"feishu://sheet/sheet-token"})

    class Context:
        def __init__(self) -> None:
            self.metadata: dict = {}

    context = Context()
    register_connector_source_items(
        context,
        source_items_from_tool_call("feishu_sheet_info", {"spreadsheet_token": "sheet-token"}),
    )
    register_connector_source_payload(context, result)
    assert context.metadata[CONNECTOR_SOURCE_ITEMS_METADATA_KEY][0]["metadata"]["acl_authority"] == (
        "connector_verified"
    )


@pytest.mark.asyncio
async def test_feishu_sheet_full_url_authorizes_url_alias_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_sheets

    url = "https://example.feishu.cn/sheets/sheet-token?sheet=sheet-1"

    async def cli_available() -> bool:
        return True

    async def run_shortcut(_args: list[str]) -> dict:
        return {"range": "sheet-1!A1:B2", "values": [["a", "b"]]}

    monkeypatch.setattr(feishu_sheets, "_feishu_cli_available", cli_available)
    monkeypatch.setattr(feishu_sheets, "_run_feishu_sheet_shortcut", run_shortcut)

    result = await feishu_sheets._feishu_sheet_read(
        "agent-1",
        {"spreadsheet_url": url, "range": "A1:B2"},
        tenant_id="tenant-1",
        current_user_id="user-1",
    )

    _assert_verified_sources(result, {url, "feishu://sheet/sheet-token"})


@pytest.mark.asyncio
async def test_feishu_drive_full_url_reads_the_parsed_token_and_authorizes_both_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_drive

    url = "https://example.feishu.cn/file/file-token?from=chat"

    async def get_token(_agent_id):
        return ("app", "tenant-access-token")

    async def download(file_token: str, _tenant_access_token: str, *, file_name=None):
        assert file_token == "file-token"
        return (b"file body", file_name or "report.txt", {"source": "drive_download"})

    monkeypatch.setattr(feishu_drive, "_get_feishu_token", get_token)
    monkeypatch.setattr(feishu_drive, "_download_drive_file", download)
    monkeypatch.setattr(feishu_drive, "_extract_file_text", lambda _content, _filename: "file body")

    result = await feishu_drive._feishu_drive_file_read(
        "agent-1",
        {"file_token": url, "file_name": "report.txt"},
        tenant_id="tenant-1",
        current_user_id="user-1",
    )

    _assert_verified_sources(result, {url, "feishu://drive/file-token"})


@pytest.mark.asyncio
async def test_feishu_base_reads_bind_base_and_table_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_base

    async def get_token(_agent_id):
        return ("app", "token")

    async def collect_items(**_kwargs):
        return ([{"record_id": "record-1", "fields": {"name": "value"}}], 1)

    async def base_get(_token: str, _path: str, _params: dict):
        return {
            "items": [{"record_id": "record-1", "fields": {"name": "value"}}],
            "total": 1,
            "has_more": False,
        }

    monkeypatch.setattr(feishu_base, "_get_feishu_token", get_token)
    monkeypatch.setattr(feishu_base, "_collect_openapi_list_items", collect_items)
    monkeypatch.setattr(feishu_base, "_base_api_get", base_get)

    tables = await feishu_base._feishu_base_table_list(
        "agent-1",
        {"base_token": "base-token"},
        tenant_id="tenant-1",
        current_user_id="user-1",
    )
    records = await feishu_base._feishu_base_record_list(
        "agent-1",
        {"base_token": "base-token", "table_id": "table-token"},
        tenant_id="tenant-1",
        current_user_id="user-1",
    )

    _assert_verified_sources(tables, {"feishu://base/base-token"})
    _assert_verified_sources(
        records,
        {"feishu://base/base-token", "feishu://base/base-token/table-token"},
    )


@pytest.mark.asyncio
async def test_feishu_wiki_list_binds_authenticated_node_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_wiki

    async def cli_available() -> bool:
        return True

    async def get_node(_node_token: str) -> dict:
        return {
            "obj_token": "doc-token",
            "obj_type": "docx",
            "space_id": "space-token",
            "parent_node_token": "",
            "has_child": True,
            "title": "Root",
            "node_token": "wiki-node",
        }

    async def api_request(_method: str, path: str, *, params=None, body=None) -> dict:
        assert path == "/open-apis/wiki/v2/spaces/space-token/nodes"
        return {
            "code": 0,
            "data": {
                "items": [
                    {
                        "title": "Child",
                        "node_token": "child-node",
                        "obj_token": "child-doc",
                        "obj_type": "docx",
                        "has_child": False,
                    }
                ],
                "has_more": False,
            },
        }

    monkeypatch.setattr(feishu_wiki, "_feishu_cli_available", cli_available)
    monkeypatch.setattr(feishu_wiki, "_feishu_wiki_get_node_via_cli", get_node)
    monkeypatch.setattr(feishu_wiki, "_feishu_cli_api_request", api_request)

    result = await feishu_wiki._feishu_wiki_list(
        "agent-1",
        {"node_token": "wiki-node"},
        tenant_id="tenant-1",
        current_user_id="user-1",
    )

    _assert_verified_sources(result, {"feishu://wiki/node/wiki-node"})


@pytest.mark.asyncio
async def test_feishu_wiki_non_doc_error_does_not_upgrade_argument_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_docs, feishu_sheets
    from app.services.connector_acl import extract_connector_source_items

    async def cli_available() -> bool:
        return True

    async def get_node(_token: str) -> dict:
        return {
            "obj_token": "sheet-token",
            "obj_type": "sheet",
            "node_token": "wiki-node",
            "title": "Broken sheet",
        }

    async def sheet_error(
        _agent_id,
        _arguments,
        *,
        tenant_id=None,
        current_user_id=None,
    ) -> str:
        assert tenant_id == "tenant-1"
        assert current_user_id == "user-1"
        return "❌ Failed to read spreadsheet"

    monkeypatch.setattr(feishu_docs, "_feishu_cli_available", cli_available)
    monkeypatch.setattr(feishu_docs, "_feishu_wiki_get_node_via_cli", get_node)
    monkeypatch.setattr(feishu_sheets, "_feishu_sheet_info", sheet_error)

    result = await feishu_docs._feishu_doc_read(
        "agent-1",
        {"document_token": "wiki-node"},
        tenant_id="tenant-1",
        current_user_id="user-1",
    )

    assert not any(
        item.get("metadata", {}).get("acl_authority") == "connector_verified"
        for item in extract_connector_source_items(result)
    )


@pytest.mark.asyncio
async def test_feishu_wiki_non_doc_success_authorizes_inner_source_and_node_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_docs, feishu_sheets
    from app.services.connector_acl import authoritative_connector_source_item, with_connector_source_items

    async def cli_available() -> bool:
        return True

    async def get_node(_token: str) -> dict:
        return {
            "obj_token": "sheet-token",
            "obj_type": "sheet",
            "node_token": "wiki-node",
            "title": "Working sheet",
        }

    async def sheet_success(
        agent_id,
        _arguments,
        *,
        tenant_id=None,
        current_user_id=None,
    ):
        return with_connector_source_items(
            "SHEET CONTENT",
            [
                authoritative_connector_source_item(
                    source="feishu://sheet/sheet-token",
                    connector="feishu",
                    resource_type="sheet",
                    tenant_id=tenant_id,
                    current_user_id=current_user_id,
                    agent_id=agent_id,
                    protected_text="SHEET CONTENT",
                )
            ],
        )

    monkeypatch.setattr(feishu_docs, "_feishu_cli_available", cli_available)
    monkeypatch.setattr(feishu_docs, "_feishu_wiki_get_node_via_cli", get_node)
    monkeypatch.setattr(feishu_sheets, "_feishu_sheet_info", sheet_success)

    result = await feishu_docs._feishu_doc_read(
        "agent-1",
        {"document_token": "wiki-node"},
        tenant_id="tenant-1",
        current_user_id="user-1",
    )

    _assert_verified_sources(
        result,
        {"feishu://doc/wiki-node", "feishu://sheet/sheet-token"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "inner_source", "module_name", "function_name", "expected_arguments"),
    [
        (
            "https://example.feishu.cn/sheets/sheet-token",
            "feishu://sheet/sheet-token",
            "feishu_sheets",
            "_feishu_sheet_read",
            {
                "spreadsheet_token": "sheet-token",
                "sheet_id": "",
                "range": "",
                "value_render_option": "",
            },
        ),
        (
            "https://example.feishu.cn/base/base-token?table=table-token",
            "feishu://base/base-token/table-token",
            "feishu_base",
            "_feishu_base_record_list",
            {
                "base_token": "base-token",
                "table_id": "table-token",
                "max_chars": None,
                "fetch_all": True,
            },
        ),
    ],
)
async def test_feishu_url_read_authorizes_sheet_and_base_aliases(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    inner_source: str,
    module_name: str,
    function_name: str,
    expected_arguments: dict,
) -> None:
    from app.services.agent_tool_domains import feishu_base, feishu_drive, feishu_sheets
    from app.services.connector_acl import authoritative_connector_source_item, with_connector_source_items

    module = {"feishu_sheets": feishu_sheets, "feishu_base": feishu_base}[module_name]

    async def inner_read(
        agent_id,
        arguments,
        *,
        tenant_id=None,
        current_user_id=None,
    ):
        assert agent_id == "agent-1"
        assert arguments == expected_arguments
        assert tenant_id == "tenant-1"
        assert current_user_id == "user-1"
        return with_connector_source_items(
            "INNER CONTENT",
            [
                authoritative_connector_source_item(
                    source=inner_source,
                    connector="feishu",
                    resource_type="sheet" if module_name == "feishu_sheets" else "base_table",
                    tenant_id=tenant_id,
                    current_user_id=current_user_id,
                    agent_id=agent_id,
                    protected_text="INNER CONTENT",
                )
            ],
        )

    monkeypatch.setattr(module, function_name, inner_read)

    result = await feishu_drive._feishu_url_read(
        "agent-1",
        {"url": url},
        tenant_id="tenant-1",
        current_user_id="user-1",
    )

    _assert_verified_sources(result, {url, inner_source})
