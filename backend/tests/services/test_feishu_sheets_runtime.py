from __future__ import annotations

import json

import pytest


def _extract_tool_error_payload(result: str) -> dict:
    marker = "<tool_error>"
    end_marker = "</tool_error>"
    start = result.index(marker) + len(marker)
    end = result.index(end_marker)
    return json.loads(result[start:end])


@pytest.mark.asyncio
async def test_feishu_sheet_info_prefers_cli_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_sheets

    async def fake_cli_available() -> bool:
        return True

    async def fake_run_feishu_cli_command(args: list[str]) -> tuple[int, str, str]:
        assert args == [
            "lark-cli",
            "sheets",
            "+info",
            "--spreadsheet-token",
            "sht-token",
            "--format",
            "json",
        ]
        return 0, json.dumps({
            "spreadsheet_token": "sht-token",
            "sheets": [{"sheet_id": "sheet-1", "title": "日报", "row_count": 100, "column_count": 8}],
        }), ""

    monkeypatch.setattr(feishu_sheets, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_sheets, "_run_feishu_cli_command", fake_run_feishu_cli_command)

    result = await feishu_sheets._feishu_sheet_info("agent-1", {"spreadsheet_token": "sht-token"})

    assert "sht-token" in result
    assert "sheet-1" in result
    assert "日报" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_feishu_sheet_read_prefers_cli_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_sheets

    async def fake_cli_available() -> bool:
        return True

    async def fake_run_feishu_cli_command(args: list[str]) -> tuple[int, str, str]:
        assert args == [
            "lark-cli",
            "sheets",
            "+read",
            "--spreadsheet-token",
            "sht-token",
            "--range",
            "sheet-1!A1:B2",
            "--format",
            "json",
        ]
        return 0, json.dumps({
            "range": "sheet-1!A1:B2",
            "values": [["日期", "状态"], ["2026-04-02", "已完成"]],
        }), ""

    monkeypatch.setattr(feishu_sheets, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_sheets, "_run_feishu_cli_command", fake_run_feishu_cli_command)

    result = await feishu_sheets._feishu_sheet_read(
        "agent-1",
        {"spreadsheet_token": "sht-token", "range": "sheet-1!A1:B2"},
    )

    assert "sheet-1!A1:B2" in result
    assert "已完成" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_feishu_sheet_read_falls_back_to_openapi_when_cli_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_sheets
    from app.services.agent_tool_domains.feishu_cli import FeishuCliError

    async def fake_cli_available() -> bool:
        return True

    async def fake_run_feishu_cli_command(*_args, **_kwargs):
        raise FeishuCliError(
            "CLI auth missing",
            error_class="not_configured",
            retryable=False,
            actionable_hint="Run lark-cli auth login before enabling CLI-backed office tools.",
        )

    async def fake_openapi_read(agent_id, arguments):
        assert agent_id == "agent-1"
        assert arguments == {"spreadsheet_token": "sht-token", "range": "sheet-1!A1:B2"}
        return "openapi sheet fallback"

    monkeypatch.setattr(feishu_sheets, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_sheets, "_run_feishu_cli_command", fake_run_feishu_cli_command)
    monkeypatch.setattr(feishu_sheets, "_feishu_sheet_read_via_openapi", fake_openapi_read)

    result = await feishu_sheets._feishu_sheet_read(
        "agent-1",
        {"spreadsheet_token": "sht-token", "range": "sheet-1!A1:B2"},
    )

    assert "openapi sheet fallback" in result
    payload = _extract_tool_error_payload(result)
    assert payload["provider"] == "lark-cli"
    assert payload["fallback_tool"] == "feishu_sheet_read:openapi"
