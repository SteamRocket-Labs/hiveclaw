from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_feishu_doc_read_handler_allows_cli_without_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.handlers import feishu as feishu_handler

    async def fake_check_feishu_configured(_agent_id: uuid.UUID) -> bool:
        return False

    async def fake_check_feishu_office_access(_agent_id: uuid.UUID) -> bool:
        return True

    async def fake_doc_read(agent_id: uuid.UUID, arguments: dict) -> str:
        assert arguments == {"document_token": "doc-token"}
        return f"doc:{agent_id}"

    monkeypatch.setattr(feishu_handler, "_check_feishu_configured", fake_check_feishu_configured)
    monkeypatch.setattr(feishu_handler, "_check_feishu_office_access", fake_check_feishu_office_access)
    monkeypatch.setattr("app.services.agent_tools._feishu_doc_read", fake_doc_read)

    result = await feishu_handler.feishu_doc_read(uuid.uuid4(), {"document_token": "doc-token"})

    assert result.startswith("doc:")


@pytest.mark.asyncio
async def test_feishu_sheet_info_handler_allows_cli_without_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.handlers import feishu as feishu_handler

    async def fake_check_feishu_configured(_agent_id: uuid.UUID) -> bool:
        return False

    async def fake_check_feishu_office_access(_agent_id: uuid.UUID) -> bool:
        return True

    async def fake_sheet_info(agent_id: uuid.UUID, arguments: dict) -> str:
        assert arguments == {"spreadsheet_token": "sht-token"}
        return f"sheet:{agent_id}"

    monkeypatch.setattr(feishu_handler, "_check_feishu_configured", fake_check_feishu_configured)
    monkeypatch.setattr(feishu_handler, "_check_feishu_office_access", fake_check_feishu_office_access)
    monkeypatch.setattr("app.services.agent_tools._feishu_sheet_info", fake_sheet_info, raising=False)

    result = await feishu_handler.feishu_sheet_info(uuid.uuid4(), {"spreadsheet_token": "sht-token"})

    assert result.startswith("sheet:")


@pytest.mark.asyncio
async def test_feishu_task_create_handler_uses_cli_only_access(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.handlers import feishu as feishu_handler

    async def fake_check_feishu_office_access(_agent_id) -> bool:
        return True

    async def fake_task_create(agent_id: uuid.UUID, arguments: dict) -> str:
        assert arguments == {"summary": "跟进客户", "due": "2026-04-03"}
        return f"task:{agent_id}"

    monkeypatch.setattr(feishu_handler, "_check_feishu_office_access", fake_check_feishu_office_access)
    monkeypatch.setattr("app.services.agent_tools._feishu_task_create", fake_task_create, raising=False)

    result = await feishu_handler.feishu_task_create(uuid.uuid4(), {"summary": "跟进客户", "due": "2026-04-03"})

    assert result.startswith("task:")


@pytest.mark.asyncio
async def test_feishu_base_record_upsert_handler_uses_cli_only_access(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.handlers import feishu as feishu_handler

    async def fake_check_feishu_office_access(_agent_id) -> bool:
        return True

    async def fake_record_upsert(agent_id: uuid.UUID, arguments: dict) -> str:
        assert arguments == {
            "base_token": "app-token",
            "table_id": "tbl_1",
            "fields": {"状态": "进行中"},
        }
        return f"base:{agent_id}"

    monkeypatch.setattr(feishu_handler, "_check_feishu_office_access", fake_check_feishu_office_access)
    monkeypatch.setattr("app.services.agent_tools._feishu_base_record_upsert", fake_record_upsert, raising=False)

    result = await feishu_handler.feishu_base_record_upsert(
        uuid.uuid4(),
        {"base_token": "app-token", "table_id": "tbl_1", "fields": {"状态": "进行中"}},
    )

    assert result.startswith("base:")


@pytest.mark.asyncio
async def test_feishu_task_complete_handler_uses_cli_only_access(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.handlers import feishu as feishu_handler

    async def fake_check_feishu_office_access(_agent_id) -> bool:
        return True

    async def fake_task_complete(agent_id: uuid.UUID, arguments: dict) -> str:
        assert arguments == {"task_id": "task_1"}
        return f"complete:{agent_id}"

    monkeypatch.setattr(feishu_handler, "_check_feishu_office_access", fake_check_feishu_office_access)
    monkeypatch.setattr("app.services.agent_tools._feishu_task_complete", fake_task_complete, raising=False)

    result = await feishu_handler.feishu_task_complete(uuid.uuid4(), {"task_id": "task_1"})

    assert result.startswith("complete:")


@pytest.mark.asyncio
async def test_feishu_base_field_list_handler_uses_cli_only_access(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.handlers import feishu as feishu_handler

    async def fake_check_feishu_office_access(_agent_id) -> bool:
        return True

    async def fake_field_list(agent_id: uuid.UUID, arguments: dict) -> str:
        assert arguments == {"base_token": "app-token", "table_id": "tbl_1"}
        return f"fields:{agent_id}"

    monkeypatch.setattr(feishu_handler, "_check_feishu_office_access", fake_check_feishu_office_access)
    monkeypatch.setattr("app.services.agent_tools._feishu_base_field_list", fake_field_list, raising=False)

    result = await feishu_handler.feishu_base_field_list(
        uuid.uuid4(),
        {"base_token": "app-token", "table_id": "tbl_1"},
    )

    assert result.startswith("fields:")


@pytest.mark.asyncio
async def test_feishu_base_record_upload_attachment_handler_uses_cli_only_access(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.handlers import feishu as feishu_handler

    async def fake_check_feishu_office_access(_agent_id) -> bool:
        return True

    async def fake_upload(agent_id: uuid.UUID, arguments: dict) -> str:
        assert arguments == {
            "base_token": "app-token",
            "table_id": "tbl_1",
            "record_id": "rec_1",
            "field_id": "附件",
            "file_path": "workspace/report.pdf",
        }
        return f"attachment:{agent_id}"

    monkeypatch.setattr(feishu_handler, "_check_feishu_office_access", fake_check_feishu_office_access)
    monkeypatch.setattr("app.services.agent_tools._feishu_base_record_upload_attachment", fake_upload, raising=False)

    result = await feishu_handler.feishu_base_record_upload_attachment(
        uuid.uuid4(),
        {
            "base_token": "app-token",
            "table_id": "tbl_1",
            "record_id": "rec_1",
            "field_id": "附件",
            "file_path": "workspace/report.pdf",
        },
    )

    assert result.startswith("attachment:")
