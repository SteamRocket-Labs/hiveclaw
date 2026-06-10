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
        return (
            0,
            json.dumps(
                {
                    "items": [
                        {"table_id": "tbl_1", "table_name": "销售日报"},
                        {"table_id": "tbl_2", "table_name": "客户跟进"},
                    ],
                    "count": 2,
                    "total": 2,
                }
            ),
            "",
        )

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
        return (
            0,
            json.dumps(
                {
                    "items": [
                        {"record_id": "rec_1", "fields": {"姓名": "张三", "状态": "已完成"}},
                    ],
                    "count": 1,
                    "total": 1,
                }
            ),
            "",
        )

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
async def test_feishu_base_record_list_requests_text_segments_and_renders_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_base

    async def fake_get_feishu_token(_agent_id):
        return "tenant", "tenant-token"

    async def fake_base_api_get(token: str, path: str, params: dict | None = None) -> dict:
        assert token == "tenant-token"
        assert path == "/bitable/v1/apps/app-token/tables/tbl_1/records"
        assert params == {"page_size": 100, "text_field_as_array": True}
        return {
            "items": [
                {
                    "record_id": "rec_bp",
                    "fields": {
                        "项目简称": [{"type": "text", "text": "HuiPat 多光子显微成像仪"}],
                        "是否有BP": [
                            {
                                "type": "url",
                                "text": "HuiPath BP(1).pdf",
                                "link": "https://b0hmj3e3npg.feishu.cn/file/BP123",
                            }
                        ],
                        "官网": {
                            "text": "公司官网",
                            "link": "https://example.com",
                        },
                    },
                }
            ],
            "total": 1,
        }

    monkeypatch.setattr(feishu_base, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_base, "_base_api_get", fake_base_api_get)

    result = await feishu_base._feishu_base_record_list(
        "agent-1",
        {"base_token": "app-token", "table_id": "tbl_1"},
    )

    assert "rec_bp" in result
    assert "项目简称: HuiPat 多光子显微成像仪" in result
    assert "是否有BP: HuiPath BP(1).pdf <https://b0hmj3e3npg.feishu.cn/file/BP123>" in result
    assert "官网: 公司官网 <https://example.com>" in result
    assert '"link"' not in result


@pytest.mark.asyncio
async def test_feishu_base_record_list_openapi_accepts_page_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_base

    async def fake_get_feishu_token(_agent_id):
        return "tenant", "tenant-token"

    async def fake_base_api_get(token: str, path: str, params: dict | None = None) -> dict:
        assert token == "tenant-token"
        assert path == "/bitable/v1/apps/app-token/tables/tbl_1/records"
        assert params == {
            "page_size": 50,
            "text_field_as_array": True,
            "page_token": "next-page",
        }
        return {
            "items": [{"record_id": "rec_2", "fields": {"公司": "后页企业"}}],
            "total": 398,
            "has_more": False,
        }

    monkeypatch.setattr(feishu_base, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_base, "_base_api_get", fake_base_api_get)

    result = await feishu_base._feishu_base_record_list(
        "agent-1",
        {
            "base_token": "app-token",
            "table_id": "tbl_1",
            "limit": 50,
            "page_token": "next-page",
        },
    )

    assert "rec_2" in result
    assert "后页企业" in result
    assert "总数：398" in result


@pytest.mark.asyncio
async def test_feishu_base_record_list_openapi_emulates_offset_with_page_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_base

    seen_params: list[dict] = []

    async def fake_get_feishu_token(_agent_id):
        return "tenant", "tenant-token"

    async def fake_base_api_get(token: str, path: str, params: dict | None = None) -> dict:
        assert token == "tenant-token"
        assert path == "/bitable/v1/apps/app-token/tables/tbl_1/records"
        seen_params.append(dict(params or {}))
        if len(seen_params) == 1:
            return {
                "items": [{"record_id": f"rec_{idx}", "fields": {"公司": f"前页{idx}"}} for idx in range(200)],
                "total": 398,
                "has_more": True,
                "page_token": "second-page",
            }
        return {
            "items": [
                {"record_id": "rec_200", "fields": {"公司": "共模半导体", "净利润": "-2,000万"}},
                {"record_id": "rec_201", "fields": {"公司": "竹间智能", "净利润": "-3,998万"}},
                *[
                    {"record_id": f"rec_{idx}", "fields": {"公司": f"后页{idx}"}}
                    for idx in range(202, 398)
                ],
            ],
            "total": 398,
            "has_more": False,
        }

    monkeypatch.setattr(feishu_base, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_base, "_base_api_get", fake_base_api_get)

    result = await feishu_base._feishu_base_record_list(
        "agent-1",
        {
            "base_token": "app-token",
            "table_id": "tbl_1",
            "offset": 200,
            "limit": 200,
        },
    )

    assert seen_params == [
        {"page_size": 200, "text_field_as_array": True},
        {"page_size": 200, "text_field_as_array": True, "page_token": "second-page"},
    ]
    assert "共模半导体" in result
    assert "竹间智能" in result
    assert "前页0" not in result
    assert "下一页 offset" not in result


@pytest.mark.asyncio
async def test_feishu_base_record_list_fetch_all_filters_negative_numeric_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_base

    seen_params: list[dict] = []

    async def fake_get_feishu_token(_agent_id):
        return "tenant", "tenant-token"

    async def fake_base_api_get(token: str, path: str, params: dict | None = None) -> dict:
        assert token == "tenant-token"
        assert path == "/bitable/v1/apps/app-token/tables/tbl_1/records"
        seen_params.append(dict(params or {}))
        if len(seen_params) == 1:
            return {
                "items": [
                    {
                        "record_id": "rec_positive",
                        "fields": {
                            "项目名称": "正利润企业",
                            "净利润": 300000000,
                            "净利润（亿元）": 3,
                            "报告期（年）": "2023",
                        },
                    }
                ],
                "total": 3,
                "has_more": True,
                "page_token": "second-page",
            }
        return {
            "items": [
                {
                    "record_id": "rec_negative",
                    "fields": {
                        "项目名称": "共模半导体-B轮",
                        "净利润": -20000000,
                        "净利润（亿元）": -0.2,
                        "报告期（年）": "2023",
                    },
                },
                {
                    "record_id": "rec_empty",
                    "fields": {
                        "项目名称": "空值企业",
                        "净利润": "",
                        "净利润（亿元）": 0,
                        "报告期（年）": "2023",
                    },
                },
            ],
            "total": 3,
            "has_more": False,
        }

    monkeypatch.setattr(feishu_base, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_base, "_base_api_get", fake_base_api_get)

    result = await feishu_base._feishu_base_record_list(
        "agent-1",
        {
            "base_token": "app-token",
            "table_id": "tbl_1",
            "fetch_all": True,
            "field_names": ["项目名称", "报告期（年）", "净利润", "净利润（亿元）"],
            "filter_field": "净利润",
            "filter_op": "<",
            "filter_value": 0,
        },
    )

    assert seen_params == [
        {"page_size": 200, "text_field_as_array": True},
        {"page_size": 200, "text_field_as_array": True, "page_token": "second-page"},
    ]
    assert "已扫描：3/3" in result
    assert "筛选命中：1" in result
    assert "共模半导体-B轮" in result
    assert "净利润: -20000000" in result
    assert "正利润企业" not in result
    assert "空值企业" not in result


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
        return (
            0,
            json.dumps(
                {
                    "items": [
                        {
                            "guid": "task_1",
                            "summary": "日报整理",
                            "url": "https://task",
                            "due_at": "2026-04-02T10:00:00Z",
                        },
                    ],
                    "has_more": False,
                }
            ),
            "",
        )

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
        return (
            0,
            json.dumps(
                {
                    "guid": "task_new_1",
                    "url": "https://applink.larkoffice.com/client/todo/detail?guid=task_new_1",
                }
            ),
            "",
        )

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
        return (
            0,
            json.dumps(
                {
                    "record": {"record_id": "rec_1", "fields": {"姓名": "张三", "状态": "已完成"}},
                    "updated": True,
                }
            ),
            "",
        )

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
        return (
            0,
            json.dumps(
                {
                    "items": [
                        {"field_id": "fld_1", "field_name": "状态", "type": 3},
                        {"field_id": "fld_2", "field_name": "负责人", "type": 11},
                    ],
                    "total": 2,
                }
            ),
            "",
        )

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
        return (
            0,
            json.dumps(
                {
                    "guid": "task_1",
                    "url": "https://applink.larkoffice.com/client/todo/detail?guid=task_1",
                    "summary": "日报整理",
                }
            ),
            "",
        )

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
async def test_feishu_task_comment_uses_current_openapi_comment_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_tasks

    captured: dict[str, object] = {}

    async def fake_get_feishu_token(_agent_id):
        return ("app", "tenant-token")

    async def fake_task_api_request(method: str, token: str, path: str, body=None, params=None):
        captured["method"] = method
        captured["token"] = token
        captured["path"] = path
        captured["body"] = body
        captured["params"] = params
        return {"comment": {"id": "comment_openapi_1"}}

    async def fake_cli_available() -> bool:
        raise AssertionError("CLI fallback should not run when OpenAPI succeeds")

    monkeypatch.setattr(feishu_tasks, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_tasks, "_task_api_request", fake_task_api_request)
    monkeypatch.setattr(feishu_tasks, "_feishu_cli_available", fake_cli_available)

    result = await feishu_tasks._feishu_task_comment(
        "agent-1",
        {"task_id": "task_1", "content": "已完成初稿，请 review。"},
    )

    assert captured == {
        "method": "POST",
        "token": "tenant-token",
        "path": "/task/v2/comments",
        "body": {
            "content": "已完成初稿，请 review。",
            "resource_type": "task",
            "resource_id": "task_1",
        },
        "params": None,
    }
    assert "comment_openapi_1" in result


@pytest.mark.asyncio
async def test_feishu_base_record_upload_attachment_uses_workspace_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
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
        return (
            0,
            json.dumps(
                {
                    "record": {"record_id": "rec_1"},
                    "attachment": {"file_token": "file_1", "name": "Q1-final.pdf"},
                    "updated": True,
                }
            ),
            "",
        )

    monkeypatch.setattr(feishu_base, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_base, "_run_feishu_cli_command", fake_run_feishu_cli_command)
    monkeypatch.setattr(
        "app.services.agent_tool_domains.feishu_base.get_settings",
        lambda: type("S", (), {"AGENT_DATA_DIR": str(workspace_root)})(),
    )

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
