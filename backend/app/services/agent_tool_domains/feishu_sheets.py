"""Feishu sheets — read-only spreadsheet info and cell access with CLI-first fallback."""

from __future__ import annotations

import json
import re
import uuid

import httpx

from app.config import get_settings
from app.services.agent_tool_domains.feishu_cli import (
    FeishuCliError,
    _feishu_cli_available,
    _run_feishu_cli_command,
)
from app.services.agent_tool_domains.feishu_helpers import _get_feishu_token
from app.tools.result_envelope import render_tool_fallback

_SINGLE_CELL_RE = re.compile(r"^[A-Za-z]+[1-9][0-9]*$")
_SHEET_URL_RE = re.compile(r"/(?:sheets|spreadsheets)/([^/?#]+)")


def _extract_spreadsheet_token(value: str) -> str:
    normalized = (value or "").strip().strip("'\"`")
    if not normalized:
        return ""
    match = _SHEET_URL_RE.search(normalized)
    if match:
        return match.group(1)
    return normalized


def _normalize_sheet_range(sheet_id: str, range_value: str) -> str:
    normalized_range = (range_value or "").strip()
    normalized_sheet_id = (sheet_id or "").strip()
    if not normalized_range:
        return normalized_sheet_id
    if "!" in normalized_range or not normalized_sheet_id:
        return normalized_range
    if _SINGLE_CELL_RE.fullmatch(normalized_range):
        return f"{normalized_sheet_id}!{normalized_range}:{normalized_range}"
    return f"{normalized_sheet_id}!{normalized_range}"


def _format_sheet_info(spreadsheet_token: str, title: str | None, sheets: list[dict]) -> str:
    lines = [f"📊 **Spreadsheet** (`{spreadsheet_token}`)"]
    if title:
        lines.append(f"标题：{title}")
    if not sheets:
        lines.append("当前表格下没有可见工作表。")
        return "\n".join(lines)
    lines.append(f"工作表（共 {len(sheets)} 个）：")
    for sheet in sheets:
        lines.append(
            f"- `{sheet.get('sheet_id', '')}` **{sheet.get('title', '(未命名)')}**"
            f" · {sheet.get('row_count', '?')} 行 × {sheet.get('column_count', '?')} 列"
        )
    return "\n".join(lines)


def _format_sheet_values(
    actual_range: str,
    values: list[list[object]],
    *,
    truncated: bool = False,
    total_rows: int | None = None,
) -> str:
    lines = [f"📊 **Sheet values** (`{actual_range}`):"]
    if not values:
        lines.append("(empty)")
    else:
        for row in values:
            lines.append(" | ".join("" if cell is None else str(cell) for cell in row))
    if truncated:
        suffix = f"（总行数 {total_rows}）" if total_rows is not None else ""
        lines.append(f"\n_(结果已截断{suffix})_")
    return "\n".join(lines)


async def _run_feishu_sheet_shortcut(args: list[str]) -> dict:
    settings = get_settings()
    command = [settings.FEISHU_CLI_BIN, *args, "--format", "json"]
    return_code, stdout, stderr = await _run_feishu_cli_command(command)
    if return_code != 0:
        raise FeishuCliError(
            stderr or stdout or "lark-cli sheets command failed.",
            error_class="provider_unavailable",
            retryable=True,
            actionable_hint="Verify lark-cli auth status, scopes, and spreadsheet arguments.",
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise FeishuCliError(
            "lark-cli sheets returned non-JSON output.",
            error_class="provider_error",
            retryable=False,
            actionable_hint="Run the same lark-cli sheets command manually and inspect the output.",
        ) from exc


async def _get_first_sheet_id_via_openapi(spreadsheet_token: str, token: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
            headers={"Authorization": f"Bearer {token}"},
        )
    payload = response.json()
    if payload.get("code") != 0:
        return ""
    sheets = payload.get("data", {}).get("sheets", [])
    if not sheets:
        return ""
    return sheets[0].get("sheet_id", "")


async def _feishu_sheet_info_via_openapi(agent_id: uuid.UUID, arguments: dict) -> str:
    spreadsheet_token = _extract_spreadsheet_token(
        arguments.get("spreadsheet_token") or arguments.get("spreadsheet_url") or ""
    )
    if not spreadsheet_token:
        return "❌ Missing required argument 'spreadsheet_token' or 'spreadsheet_url'"

    creds = await _get_feishu_token(agent_id)
    if not creds:
        return "❌ Agent has no Feishu channel configured."
    _, token = creds
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=20) as client:
        spreadsheet_resp = await client.get(
            f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}",
            headers=headers,
        )
        sheets_resp = await client.get(
            f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
            headers=headers,
        )

    spreadsheet_payload = spreadsheet_resp.json()
    if spreadsheet_payload.get("code") != 0:
        return f"❌ Failed to read spreadsheet: {spreadsheet_payload.get('msg')} (code {spreadsheet_payload.get('code')})"

    sheets_payload = sheets_resp.json()
    if sheets_payload.get("code") != 0:
        return f"❌ Failed to list sheets: {sheets_payload.get('msg')} (code {sheets_payload.get('code')})"

    spreadsheet_data = spreadsheet_payload.get("data", {})
    spreadsheet = spreadsheet_data.get("spreadsheet", spreadsheet_data)
    sheets = sheets_payload.get("data", {}).get("sheets", [])
    return _format_sheet_info(
        spreadsheet_token=spreadsheet.get("spreadsheet_token", spreadsheet_token),
        title=spreadsheet.get("title"),
        sheets=sheets,
    )


async def _feishu_sheet_info(agent_id: uuid.UUID, arguments: dict) -> str:
    spreadsheet_token = _extract_spreadsheet_token(
        arguments.get("spreadsheet_token") or arguments.get("spreadsheet_url") or ""
    )
    if not spreadsheet_token:
        return "❌ Missing required argument 'spreadsheet_token' or 'spreadsheet_url'"

    if not await _feishu_cli_available():
        return await _feishu_sheet_info_via_openapi(agent_id, arguments)

    cli_args = ["sheets", "+info", "--spreadsheet-token", spreadsheet_token]
    if arguments.get("spreadsheet_url"):
        cli_args = ["sheets", "+info", "--url", str(arguments["spreadsheet_url"]).strip()]

    try:
        payload = await _run_feishu_sheet_shortcut(cli_args)
        spreadsheet = payload.get("spreadsheet", payload)
        if isinstance(spreadsheet, dict) and "spreadsheet" in spreadsheet:
            spreadsheet = spreadsheet["spreadsheet"]
        sheets_payload = payload.get("sheets", [])
        sheets = sheets_payload.get("sheets", []) if isinstance(sheets_payload, dict) else sheets_payload
        return _format_sheet_info(
            spreadsheet_token=spreadsheet.get("spreadsheet_token", payload.get("spreadsheet_token", spreadsheet_token)),
            title=spreadsheet.get("title"),
            sheets=sheets,
        )
    except FeishuCliError as exc:
        fallback_result = await _feishu_sheet_info_via_openapi(agent_id, arguments)
        return render_tool_fallback(
            tool_name="feishu_sheet_info",
            error_class=exc.error_class,
            message=str(exc),
            fallback_tool="feishu_sheet_info:openapi",
            fallback_result=fallback_result,
            provider="lark-cli",
            retryable=exc.retryable,
            actionable_hint=exc.actionable_hint,
        )


async def _feishu_sheet_read_via_openapi(agent_id: uuid.UUID, arguments: dict) -> str:
    spreadsheet_token = _extract_spreadsheet_token(
        arguments.get("spreadsheet_token") or arguments.get("spreadsheet_url") or ""
    )
    if not spreadsheet_token:
        return "❌ Missing required argument 'spreadsheet_token' or 'spreadsheet_url'"

    creds = await _get_feishu_token(agent_id)
    if not creds:
        return "❌ Agent has no Feishu channel configured."
    _, token = creds
    headers = {"Authorization": f"Bearer {token}"}

    sheet_id = (arguments.get("sheet_id") or "").strip()
    read_range = _normalize_sheet_range(sheet_id, str(arguments.get("range") or ""))
    if not read_range:
        first_sheet_id = await _get_first_sheet_id_via_openapi(spreadsheet_token, token)
        if not first_sheet_id:
            return "❌ No sheets found in this spreadsheet."
        read_range = first_sheet_id

    params: dict[str, str] = {}
    if arguments.get("value_render_option"):
        params["valueRenderOption"] = str(arguments["value_render_option"])

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{read_range}",
            headers=headers,
            params=params,
        )

    payload = response.json()
    if payload.get("code") != 0:
        return f"❌ Failed to read sheet: {payload.get('msg')} (code {payload.get('code')})"

    data = payload.get("data", {})
    value_range = data.get("valueRange") or data.get("value_range") or data
    actual_range = value_range.get("range", read_range)
    values = value_range.get("values", [])
    truncated = bool(value_range.get("truncated", False))
    total_rows = value_range.get("total_rows")
    return _format_sheet_values(actual_range, values, truncated=truncated, total_rows=total_rows)


async def _feishu_sheet_read(agent_id: uuid.UUID, arguments: dict) -> str:
    spreadsheet_token = _extract_spreadsheet_token(
        arguments.get("spreadsheet_token") or arguments.get("spreadsheet_url") or ""
    )
    if not spreadsheet_token:
        return "❌ Missing required argument 'spreadsheet_token' or 'spreadsheet_url'"

    sheet_id = (arguments.get("sheet_id") or "").strip()
    read_range = _normalize_sheet_range(sheet_id, str(arguments.get("range") or ""))

    if not await _feishu_cli_available():
        return await _feishu_sheet_read_via_openapi(agent_id, arguments)

    cli_args = ["sheets", "+read", "--spreadsheet-token", spreadsheet_token]
    if arguments.get("spreadsheet_url"):
        cli_args = ["sheets", "+read", "--url", str(arguments["spreadsheet_url"]).strip()]
    if sheet_id:
        cli_args.extend(["--sheet-id", sheet_id])
    if read_range:
        cli_args.extend(["--range", read_range])
    if arguments.get("value_render_option"):
        cli_args.extend(["--value-render-option", str(arguments["value_render_option"])])

    try:
        payload = await _run_feishu_sheet_shortcut(cli_args)
        actual_range = payload.get("range", read_range)
        values = payload.get("values", [])
        truncated = bool(payload.get("truncated", False))
        total_rows = payload.get("total_rows")
        return _format_sheet_values(actual_range, values, truncated=truncated, total_rows=total_rows)
    except FeishuCliError as exc:
        fallback_result = await _feishu_sheet_read_via_openapi(agent_id, arguments)
        return render_tool_fallback(
            tool_name="feishu_sheet_read",
            error_class=exc.error_class,
            message=str(exc),
            fallback_tool="feishu_sheet_read:openapi",
            fallback_result=fallback_result,
            provider="lark-cli",
            retryable=exc.retryable,
            actionable_hint=exc.actionable_hint,
        )
