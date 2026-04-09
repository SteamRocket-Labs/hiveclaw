from __future__ import annotations

import json

import pytest


# All CLI-path tests need _get_feishu_token to return None so the code
# skips the OpenAPI path and falls through to the CLI fallback.
@pytest.fixture(autouse=True)
def _no_openapi_token(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get_feishu_token(_agent_id):
        return None

    monkeypatch.setattr(
        "app.services.agent_tool_domains.feishu_base._get_feishu_token",
        _fake_get_feishu_token,
    )
    monkeypatch.setattr(
        "app.services.agent_tool_domains.feishu_tasks._get_feishu_token",
        _fake_get_feishu_token,
    )


def _extract_tool_error_payload(result: str) -> dict:
    marker = "<tool_error>"
    end_marker = "</tool_error>"
    start = result.index(marker) + len(marker)
    end = result.index(end_marker)
    return json.loads(result[start:end])


@pytest.mark.asyncio
async def test_feishu_base_table_list_prefers_cli_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_base

    async def fake_cli_available() -> bool:
        return True

    async def fake_run_feishu_cli_command(args: list[str]) -> tuple[int, str, str]:
        assert args == [
            "lark-cli",
            "base",
            "+table-list",
            "--base-token",
            "app-token",
            "--offset",
            "0",
            "--limit",
            "50",
            "--format",
            "json",
        ]
        return 0, json.dumps({
            "items": [
                {"table_id": "tbl_1", "table_name": "销售日报"},
                {"table_id": "tbl_2", "table_name": "客户跟进"},
            ],
            "count": 2,
            "total": 2,
        }), ""

    monkeypatch.setattr(feishu_base, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_base, "_run_feishu_cli_command", fake_run_feishu_cli_command)

    result = await feishu_base._feishu_base_table_list("agent-1", {"base_token": "app-token"})

    assert "销售日报" in result
    assert "tbl_2" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_feishu_base_record_list_prefers_cli_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_base

    async def fake_cli_available() -> bool:
        return True

    async def fake_run_feishu_cli_command(args: list[str]) -> tuple[int, str, str]:
        assert args == [
            "lark-cli",
            "base",
            "+record-list",
            "--base-token",
            "app-token",
            "--table-id",
            "tbl_1",
            "--offset",
            "0",
            "--limit",
            "100",
            "--format",
            "json",
        ]
        return 0, json.dumps({
            "items": [
                {"record_id": "rec_1", "fields": {"姓名": "张三", "状态": "已完成"}},
            ],
            "count": 1,
            "total": 1,
        }), ""

    monkeypatch.setattr(feishu_base, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_base, "_run_feishu_cli_command", fake_run_feishu_cli_command)

    result = await feishu_base._feishu_base_record_list(
        "agent-1",
        {"base_token": "app-token", "table_id": "tbl_1"},
    )

    assert "rec_1" in result
    assert "张三" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_feishu_task_list_uses_user_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_tasks

    async def fake_cli_available() -> bool:
        return True

    async def fake_run_feishu_cli_command(args: list[str]) -> tuple[int, str, str]:
        assert args == [
            "lark-cli",
            "task",
            "+get-my-tasks",
            "--query",
            "日报",
            "--as",
            "user",
            "--format",
            "json",
        ]
        return 0, json.dumps({
            "items": [
                {"guid": "task_1", "summary": "日报整理", "url": "https://task", "due_at": "2026-04-02T10:00:00Z"},
            ],
            "has_more": False,
        }), ""

    monkeypatch.setattr(feishu_tasks, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_tasks, "_run_feishu_cli_command", fake_run_feishu_cli_command)

    result = await feishu_tasks._feishu_task_list("agent-1", {"query": "日报"})

    assert "task_1" in result
    assert "日报整理" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_feishu_task_list_returns_structured_error_when_cli_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_tasks

    async def fake_cli_available() -> bool:
        return False

    monkeypatch.setattr(feishu_tasks, "_feishu_cli_available", fake_cli_available)

    result = await feishu_tasks._feishu_task_list("agent-1", {"query": "日报"})

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "feishu_task_list"
    assert payload["error_class"] == "not_configured"


@pytest.mark.asyncio
async def test_feishu_task_create_uses_user_identity_and_returns_created_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_tasks

    async def fake_cli_available() -> bool:
        return True

    async def fake_run_feishu_cli_command(args: list[str]) -> tuple[int, str, str]:
        assert args == [
            "lark-cli",
            "task",
            "+create",
            "--summary",
            "周报整理",
            "--description",
            "请在今晚前完成周报整理",
            "--assignee",
            "ou_user_1",
            "--due",
            "2026-04-03",
            "--tasklist-id",
            "https://applink.larkoffice.com/client/todo/task_list?guid=list_1",
            "--idempotency-key",
            "task-create-1",
            "--as",
            "user",
            "--format",
            "json",
        ]
        return 0, json.dumps({
            "guid": "task_new_1",
            "url": "https://applink.larkoffice.com/client/todo/detail?guid=task_new_1",
        }), ""

    monkeypatch.setattr(feishu_tasks, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_tasks, "_run_feishu_cli_command", fake_run_feishu_cli_command)

    result = await feishu_tasks._feishu_task_create(
        "agent-1",
        {
            "summary": "周报整理",
            "description": "请在今晚前完成周报整理",
            "assignee_open_id": "ou_user_1",
            "due": "2026-04-03",
            "tasklist_id": "https://applink.larkoffice.com/client/todo/task_list?guid=list_1",
            "idempotency_key": "task-create-1",
        },
    )

    assert "task_new_1" in result
    assert "周报整理" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_feishu_task_create_requires_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_tasks

    async def fake_cli_available() -> bool:
        return True

    monkeypatch.setattr(feishu_tasks, "_feishu_cli_available", fake_cli_available)

    result = await feishu_tasks._feishu_task_create("agent-1", {"description": "missing summary"})

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "feishu_task_create"
    assert payload["error_class"] == "invalid_input"


@pytest.mark.asyncio
async def test_feishu_base_record_upsert_supports_update(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_base

    async def fake_cli_available() -> bool:
        return True

    async def fake_run_feishu_cli_command(args: list[str]) -> tuple[int, str, str]:
        assert args == [
            "lark-cli",
            "base",
            "+record-upsert",
            "--base-token",
            "app-token",
            "--table-id",
            "tbl_1",
            "--record-id",
            "rec_1",
            "--json",
            '{"姓名":"张三","状态":"已完成"}',
            "--format",
            "json",
        ]
        return 0, json.dumps({
            "record": {"record_id": "rec_1", "fields": {"姓名": "张三", "状态": "已完成"}},
            "updated": True,
        }), ""

    monkeypatch.setattr(feishu_base, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_base, "_run_feishu_cli_command", fake_run_feishu_cli_command)

    result = await feishu_base._feishu_base_record_upsert(
        "agent-1",
        {
            "base_token": "app-token",
            "table_id": "tbl_1",
            "record_id": "rec_1",
            "fields": {"姓名": "张三", "状态": "已完成"},
        },
    )

    assert "rec_1" in result
    assert "已完成" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_feishu_base_record_upsert_requires_fields_object(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_base

    async def fake_cli_available() -> bool:
        return True

    monkeypatch.setattr(feishu_base, "_feishu_cli_available", fake_cli_available)

    result = await feishu_base._feishu_base_record_upsert(
        "agent-1",
        {"base_token": "app-token", "table_id": "tbl_1", "fields": ["bad"]},
    )

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "feishu_base_record_upsert"
    assert payload["error_class"] == "invalid_input"


@pytest.mark.asyncio
async def test_feishu_base_field_list_prefers_cli_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_base

    async def fake_cli_available() -> bool:
        return True

    async def fake_run_feishu_cli_command(args: list[str]) -> tuple[int, str, str]:
        assert args == [
            "lark-cli",
            "base",
            "+field-list",
            "--base-token",
            "app-token",
            "--table-id",
            "tbl_1",
            "--offset",
            "0",
            "--limit",
            "100",
            "--format",
            "json",
        ]
        return 0, json.dumps({
            "items": [
                {"field_id": "fld_1", "field_name": "状态", "type": 3},
                {"field_id": "fld_2", "field_name": "负责人", "type": 11},
            ],
            "total": 2,
        }), ""

    monkeypatch.setattr(feishu_base, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_base, "_run_feishu_cli_command", fake_run_feishu_cli_command)

    result = await feishu_base._feishu_base_field_list(
        "agent-1",
        {"base_token": "app-token", "table_id": "tbl_1"},
    )

    assert "状态" in result
    assert "fld_2" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_feishu_task_complete_marks_task_done(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_tasks

    async def fake_cli_available() -> bool:
        return True

    async def fake_run_feishu_cli_command(args: list[str]) -> tuple[int, str, str]:
        assert args == [
            "lark-cli",
            "task",
            "+complete",
            "--task-id",
            "task_1",
            "--as",
            "user",
            "--format",
            "json",
        ]
        return 0, json.dumps({
            "guid": "task_1",
            "url": "https://applink.larkoffice.com/client/todo/detail?guid=task_1",
            "summary": "日报整理",
        }), ""

    monkeypatch.setattr(feishu_tasks, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_tasks, "_run_feishu_cli_command", fake_run_feishu_cli_command)

    result = await feishu_tasks._feishu_task_complete("agent-1", {"task_id": "task_1"})

    assert "task_1" in result
    assert "日报整理" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_feishu_task_comment_adds_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_tasks

    async def fake_cli_available() -> bool:
        return True

    async def fake_run_feishu_cli_command(args: list[str]) -> tuple[int, str, str]:
        assert args == [
            "lark-cli",
            "task",
            "+comment",
            "--task-id",
            "task_1",
            "--content",
            "已完成初稿，请 review。",
            "--as",
            "user",
            "--format",
            "json",
        ]
        return 0, json.dumps({"id": "comment_1"}), ""

    monkeypatch.setattr(feishu_tasks, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_tasks, "_run_feishu_cli_command", fake_run_feishu_cli_command)

    result = await feishu_tasks._feishu_task_comment(
        "agent-1",
        {"task_id": "task_1", "content": "已完成初稿，请 review。"},
    )

    assert "comment_1" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_feishu_base_record_upload_attachment_uses_workspace_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from app.services.agent_tool_domains import feishu_base

    agent_id = "agent-1"
    workspace_root = tmp_path / "agents"
    workspace = workspace_root / agent_id / "workspace"
    workspace.mkdir(parents=True)
    source_file = workspace / "report.pdf"
    source_file.write_text("pdf", encoding="utf-8")

    async def fake_cli_available() -> bool:
        return True

    async def fake_run_feishu_cli_command(args: list[str]) -> tuple[int, str, str]:
        assert args == [
            "lark-cli",
            "base",
            "+record-upload-attachment",
            "--base-token",
            "app-token",
            "--table-id",
            "tbl_1",
            "--record-id",
            "rec_1",
            "--field-id",
            "附件",
            "--file",
            str(source_file),
            "--name",
            "Q1-final.pdf",
            "--format",
            "json",
        ]
        return 0, json.dumps({
            "record": {"record_id": "rec_1"},
            "attachment": {"file_token": "file_1", "name": "Q1-final.pdf"},
            "updated": True,
        }), ""

    monkeypatch.setattr(feishu_base, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_base, "_run_feishu_cli_command", fake_run_feishu_cli_command)
    monkeypatch.setattr("app.services.agent_tool_domains.feishu_base.get_settings", lambda: type("S", (), {"AGENT_DATA_DIR": str(workspace_root)})())

    result = await feishu_base._feishu_base_record_upload_attachment(
        agent_id,
        {
            "base_token": "app-token",
            "table_id": "tbl_1",
            "record_id": "rec_1",
            "field_id": "附件",
            "file_path": "workspace/report.pdf",
            "name": "Q1-final.pdf",
        },
    )

    assert "file_1" in result
    assert "Q1-final.pdf" in result
    assert "<tool_error>" not in result
