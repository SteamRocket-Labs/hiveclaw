"""Feishu Base — multi-dimensional table operations via Open API (with CLI fallback)."""

from __future__ import annotations

import json
import logging
import mimetypes
import re
from pathlib import Path

import httpx

from app.config import get_settings
from app.services.agent_tool_domains.feishu_cli import FeishuCliError, _feishu_cli_available, _run_feishu_cli_command
from app.services.agent_tool_domains.feishu_helpers import _get_feishu_token
from app.tools.result_envelope import render_tool_error

logger = logging.getLogger(__name__)

FEISHU_API = "https://open.feishu.cn/open-apis"


# ── Render helpers (unchanged) ───────────────────────────────────────


def _render_base_tables(base_token: str, items: list[dict], *, total: int | None = None) -> str:
    lines = [f"🗂️ **Feishu Base tables** (`{base_token}`)"]
    if total is not None:
        lines.append(f"总数：{total}")
    if not items:
        lines.append("当前 Base 下没有数据表。")
        return "\n".join(lines)
    for item in items:
        lines.append(f"- `{item.get('table_id', '')}` **{item.get('table_name', item.get('name', '(未命名)'))}**")
    return "\n".join(lines)


def _first_nonempty_string(value: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ""


def _format_base_link_value(text: str, link: str) -> str:
    if text and text != link:
        return f"{text} <{link}>"
    return link


def _looks_like_text_segments(items: list) -> bool:
    return bool(items) and all(
        isinstance(item, dict) and "type" in item and ("text" in item or "link" in item or "url" in item)
        for item in items
    )


def _format_base_field_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        separator = "" if _looks_like_text_segments(value) else "; "
        return separator.join(_format_base_field_value(item) for item in value)
    if isinstance(value, dict):
        link = _first_nonempty_string(value, ("link", "url", "href"))
        text = _first_nonempty_string(value, ("text", "name", "title", "file_name"))
        if link:
            return _format_base_link_value(text, link)
        if text:
            return text
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _render_base_records(
    table_id: str,
    items: list[dict],
    *,
    total: int | None = None,
    has_more: bool | None = None,
    next_page_token: str | None = None,
    next_offset: int | None = None,
    field_names: list[str] | None = None,
    scanned_count: int | None = None,
    matched_count: int | None = None,
) -> str:
    lines = [f"📋 **Feishu Base records** (`{table_id}`)"]
    if total is not None:
        lines.append(f"总数：{total}")
    if scanned_count is not None:
        total_text = str(total) if total is not None else "?"
        lines.append(f"已扫描：{scanned_count}/{total_text}")
    if matched_count is not None:
        lines.append(f"筛选命中：{matched_count}")
    lines.append(f"本页返回：{len(items)}")
    if has_more:
        if next_page_token:
            lines.append(f"下一页 page_token：`{next_page_token}`")
        if next_offset is not None:
            lines.append(f"下一页 offset：{next_offset}")
    if not items:
        lines.append("当前表下没有记录。")
        return "\n".join(lines)
    for item in items:
        lines.append(f"- `{item.get('record_id', '')}`")
        fields = item.get("fields", {})
        if isinstance(fields, dict) and fields:
            if field_names:
                rendered_fields = {field_name: fields.get(field_name) for field_name in field_names}
            else:
                rendered_fields = fields
            for field_name, field_value in rendered_fields.items():
                lines.append(f"  - {field_name}: {_format_base_field_value(field_value)}")
        elif fields:
            lines.append(f"  - Fields: {_format_base_field_value(fields)}")
    return "\n".join(lines)


def _payload_has_more(payload: dict, *, returned_count: int, offset: int = 0) -> bool:
    if payload.get("has_more"):
        return True
    total = payload.get("total")
    return isinstance(total, int) and offset + returned_count < total


def _base_record_list_params(*, page_size: int, view_id: str = "", page_token: str = "") -> dict:
    params: dict = {"page_size": page_size, "text_field_as_array": True}
    if view_id:
        params["view_id"] = view_id
    if page_token:
        params["page_token"] = page_token
    return params


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "all"}
    return False


def _normalize_field_names(value) -> list[str] | None:
    if not value:
        return None
    if isinstance(value, str):
        fields = [part.strip() for part in re.split(r"[,，\n]", value)]
    elif isinstance(value, list):
        fields = [str(part).strip() for part in value]
    else:
        fields = [str(value).strip()]
    normalized = [field for field in fields if field]
    return normalized or None


def _project_record_fields(item: dict, field_names: list[str] | None) -> dict:
    if not field_names:
        return item
    fields = item.get("fields")
    if not isinstance(fields, dict):
        return item
    projected = {field_name: fields.get(field_name) for field_name in field_names}
    return {**item, "fields": projected}


def _parse_number(value) -> float | None:
    text = _format_base_field_value(value).strip()
    if not text:
        return None
    text = text.replace(",", "").replace("，", "").replace("−", "-").replace("﹣", "-").replace("－", "-")
    multiplier = 1.0
    if text.endswith("亿"):
        multiplier = 100_000_000.0
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = 10_000.0
        text = text[:-1]
    match = re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", text.strip())
    if not match:
        return None
    return float(text) * multiplier


def _record_matches_filter(item: dict, *, field_name: str, op: str, expected) -> bool:
    fields = item.get("fields")
    if not isinstance(fields, dict):
        return False
    actual = fields.get(field_name)
    actual_text = _format_base_field_value(actual).strip()
    normalized_op = (op or "").strip().lower()

    if normalized_op in {"empty", "is_empty"}:
        return not actual_text
    if normalized_op in {"not_empty", "is_not_empty"}:
        return bool(actual_text)
    if normalized_op in {"contains", "not_contains"}:
        expected_text = _format_base_field_value(expected)
        contains = expected_text in actual_text
        return contains if normalized_op == "contains" else not contains
    if normalized_op in {"=", "==", "eq", "!=", "ne"}:
        expected_text = _format_base_field_value(expected).strip()
        equal = actual_text == expected_text
        return equal if normalized_op in {"=", "==", "eq"} else not equal

    actual_num = _parse_number(actual)
    expected_num = _parse_number(expected)
    if actual_num is None or expected_num is None:
        return False
    if normalized_op in {"<", "lt"}:
        return actual_num < expected_num
    if normalized_op in {"<=", "lte"}:
        return actual_num <= expected_num
    if normalized_op in {">", "gt"}:
        return actual_num > expected_num
    if normalized_op in {">=", "gte"}:
        return actual_num >= expected_num
    return False


def _filter_records(items: list[dict], *, field_name: str, op: str, expected) -> list[dict]:
    if not field_name or not op:
        return items
    return [
        item
        for item in items
        if _record_matches_filter(item, field_name=field_name, op=op, expected=expected)
    ]


def _render_base_upsert(table_id: str, payload: dict) -> str:
    record = payload.get("record", {})
    record_id = record.get("record_id") or record.get("id", "")
    status = "updated" if payload.get("updated") else "created"
    lines = [f"✅ **Feishu Base record {status}** (`{table_id}`)"]
    if record_id:
        lines.append(f"- Record ID: `{record_id}`")
    lines.append(f"- Fields: {json.dumps(record.get('fields', {}), ensure_ascii=False)}")
    return "\n".join(lines)


def _render_base_app_create(payload: dict) -> str:
    app = payload.get("app", payload)
    app_token = app.get("app_token") or app.get("token", "")
    url = app.get("url", "")
    lines = [f"✅ **Feishu Base created** {app.get('name', '')}".rstrip()]
    if app_token:
        lines.append(f"- Base Token: `{app_token}`")
    if url:
        lines.append(f"- URL: {url}")
    return "\n".join(lines)


def _render_base_fields(table_id: str, items: list[dict], *, total: int | None = None) -> str:
    lines = [f"🧩 **Feishu Base fields** (`{table_id}`)"]
    if total is not None:
        lines.append(f"总数：{total}")
    if not items:
        lines.append("当前表下没有字段。")
        return "\n".join(lines)
    for item in items:
        lines.append(
            f"- `{item.get('field_id', '')}` **{item.get('field_name', '(未命名字段)')}** · type: {item.get('type', '')}"
        )
    return "\n".join(lines)


def _render_base_field_create(table_id: str, field: dict) -> str:
    field_id = field.get("field_id", "")
    field_name = field.get("field_name", "(未命名)")
    field_type = field.get("type", "")
    lines = [f"✅ **Feishu Base field created** (`{table_id}`)"]
    if field_id:
        lines.append(f"- Field ID: `{field_id}`")
    lines.append(f"- Field Name: **{field_name}**")
    if field_type:
        lines.append(f"- Type: {field_type}")
    return "\n".join(lines)


def _render_base_attachment_upload(table_id: str, payload: dict) -> str:
    record = payload.get("record", {})
    attachment = payload.get("attachment", {})
    record_id = record.get("record_id") or record.get("id", "")
    file_token = attachment.get("file_token", "")
    name = attachment.get("name", "")
    lines = [f"📎 **Feishu Base attachment uploaded** (`{table_id}`)"]
    if record_id:
        lines.append(f"- Record ID: `{record_id}`")
    if file_token:
        lines.append(f"- File Token: `{file_token}`")
    if name:
        lines.append(f"- File Name: {name}")
    return "\n".join(lines)


def _render_base_record_delete(table_id: str, record_id: str) -> str:
    return "\n".join(
        [
            f"🗑️ **Feishu Base record deleted** (`{table_id}`)",
            f"- Record ID: `{record_id}`",
        ]
    )


# ── Shared helpers ───────────────────────────────────────────────────


def _render_invalid_input(message: str, *, tool_name: str, actionable_hint: str | None = None) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class="invalid_input",
        message=message,
        provider="feishu_openapi",
        retryable=False,
        actionable_hint=actionable_hint,
    )


def _resolve_workspace_file(agent_id, file_path: str) -> Path:
    settings = get_settings()
    workspace_root = Path(settings.AGENT_DATA_DIR).resolve() / str(agent_id)
    candidate = (workspace_root / file_path).resolve()
    if not str(candidate).startswith(str(workspace_root)):
        raise ValueError("file_path must stay inside the agent workspace")
    return candidate


def _not_configured_error(tool_name: str) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class="not_configured",
        message="Feishu is not configured for this agent.",
        provider="feishu_openapi",
        actionable_hint="Configure Feishu App credentials in Enterprise Settings → Channels.",
    )


# ── CLI fallback helpers (kept for backward compat) ──────────────────


async def _run_feishu_base_shortcut(args: list[str]) -> dict:
    settings = get_settings()
    cli_bin = getattr(settings, "FEISHU_CLI_BIN", "lark-cli") or "lark-cli"
    command = [cli_bin, *args, "--format", "json"]
    return_code, stdout, stderr = await _run_feishu_cli_command(command)
    if return_code != 0:
        raise FeishuCliError(
            stderr or stdout or "lark-cli base command failed.",
            error_class="provider_unavailable",
            retryable=True,
            actionable_hint="Verify lark-cli auth status, Base scopes, and base/table arguments.",
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise FeishuCliError(
            "lark-cli base returned non-JSON output.",
            error_class="provider_error",
            retryable=False,
            actionable_hint="Run the same lark-cli base command manually and inspect the output.",
        ) from exc


# ── OpenAPI implementations ──────────────────────────────────────────


async def _base_api_get(token: str, path: str, params: dict | None = None) -> dict:
    """GET request to Feishu Bitable API with standard error handling."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{FEISHU_API}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu API error: {data.get('msg')} (code {data.get('code')})")
    return data.get("data", {})


async def _base_api_post(token: str, path: str, body: dict) -> dict:
    """POST request to Feishu Bitable API."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{FEISHU_API}{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json=body,
        )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu API error: {data.get('msg')} (code {data.get('code')})")
    return data.get("data", {})


async def _base_api_put(token: str, path: str, body: dict) -> dict:
    """PUT request to Feishu Bitable API."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.put(
            f"{FEISHU_API}{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json=body,
        )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu API error: {data.get('msg')} (code {data.get('code')})")
    return data.get("data", {})


async def _base_api_delete(token: str, path: str) -> dict:
    """DELETE request to Feishu Bitable API."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.delete(
            f"{FEISHU_API}{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu API error: {data.get('msg')} (code {data.get('code')})")
    return data.get("data", {})


# ── Public entry points (OpenAPI first, CLI fallback) ────────────────


async def _feishu_base_table_list(agent_id, arguments: dict) -> str:
    base_token = str(arguments.get("base_token") or "").strip()
    if not base_token:
        return "❌ Missing required argument 'base_token'"

    limit = min(max(1, int(arguments.get("limit", 50))), 100)

    # Try OpenAPI first
    creds = await _get_feishu_token(agent_id)
    if creds:
        _, token = creds
        try:
            data = await _base_api_get(token, f"/bitable/v1/apps/{base_token}/tables", {"page_size": limit})
            items = [
                {"table_id": t.get("table_id", ""), "table_name": t.get("name", "")} for t in data.get("items", [])
            ]
            return _render_base_tables(base_token, items, total=data.get("total"))
        except Exception as exc:
            logger.warning("[FeishuBase] OpenAPI table_list failed, trying CLI: %s", exc)

    # CLI fallback
    if not await _feishu_cli_available():
        return _not_configured_error("feishu_base_table_list")

    offset = max(0, int(arguments.get("offset", 0)))
    payload = await _run_feishu_base_shortcut(
        ["base", "+table-list", "--base-token", base_token, "--offset", str(offset), "--limit", str(limit)]
    )
    return _render_base_tables(base_token, payload.get("items", []), total=payload.get("total"))


async def _feishu_base_app_create(agent_id, arguments: dict) -> str:
    name = str(arguments.get("name") or "").strip()
    if not name:
        return _render_invalid_input("Missing required argument 'name'.", tool_name="feishu_base_app_create")

    body: dict = {"name": name}
    folder_token = str(arguments.get("folder_token") or "").strip()
    if folder_token:
        body["folder_token"] = folder_token
    time_zone = str(arguments.get("time_zone") or "").strip()
    if time_zone:
        body["time_zone"] = time_zone

    creds = await _get_feishu_token(agent_id)
    if creds:
        _, token = creds
        data = await _base_api_post(token, "/bitable/v1/apps", body)
        return _render_base_app_create(data)

    if not await _feishu_cli_available():
        return _not_configured_error("feishu_base_app_create")

    command = ["base", "+app-create", "--name", name]
    if folder_token:
        command.extend(["--folder-token", folder_token])
    if time_zone:
        command.extend(["--time-zone", time_zone])
    payload = await _run_feishu_base_shortcut(command)
    return _render_base_app_create(payload)


async def _feishu_base_field_list(agent_id, arguments: dict) -> str:
    base_token = str(arguments.get("base_token") or "").strip()
    table_id = str(arguments.get("table_id") or "").strip()
    if not base_token:
        return _render_invalid_input("Missing required argument 'base_token'.", tool_name="feishu_base_field_list")
    if not table_id:
        return _render_invalid_input("Missing required argument 'table_id'.", tool_name="feishu_base_field_list")

    limit = min(max(1, int(arguments.get("limit", 100))), 200)

    creds = await _get_feishu_token(agent_id)
    if creds:
        _, token = creds
        try:
            data = await _base_api_get(
                token, f"/bitable/v1/apps/{base_token}/tables/{table_id}/fields", {"page_size": limit}
            )
            return _render_base_fields(table_id, data.get("items", []), total=data.get("total"))
        except Exception as exc:
            logger.warning("[FeishuBase] OpenAPI field_list failed, trying CLI: %s", exc)

    if not await _feishu_cli_available():
        return _not_configured_error("feishu_base_field_list")

    offset = max(0, int(arguments.get("offset", 0)))
    payload = await _run_feishu_base_shortcut(
        [
            "base",
            "+field-list",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--offset",
            str(offset),
            "--limit",
            str(limit),
        ]
    )
    return _render_base_fields(table_id, payload.get("items", []), total=payload.get("total"))


async def _feishu_base_field_create(agent_id, arguments: dict) -> str:
    base_token = str(arguments.get("base_token") or "").strip()
    table_id = str(arguments.get("table_id") or "").strip()
    field_name = str(arguments.get("field_name") or "").strip()
    field_type = int(arguments.get("type", 0) or 0)
    tn = "feishu_base_field_create"
    if not base_token:
        return _render_invalid_input("Missing required argument 'base_token'.", tool_name=tn)
    if not table_id:
        return _render_invalid_input("Missing required argument 'table_id'.", tool_name=tn)
    if not field_name:
        return _render_invalid_input("Missing required argument 'field_name'.", tool_name=tn)
    if not field_type:
        return _render_invalid_input(
            "Missing required argument 'type' (field type code).",
            tool_name=tn,
            actionable_hint="Common types: 1=Text, 2=Number, 3=SingleSelect, 4=MultiSelect, 5=Date, 7=Checkbox, 11=Person, 13=Phone, 15=URL, 17=Attachment, 18=Link, 20=Formula, 21=DuplexLink, 22=Location, 23=GroupChat, 1001=CreatedTime, 1002=ModifiedTime, 1003=Creator, 1004=Modifier.",
        )

    body: dict = {"field_name": field_name, "type": field_type}
    # Optional property config (e.g. options for select fields)
    property_config = arguments.get("property")
    if isinstance(property_config, dict):
        body["property"] = property_config

    creds = await _get_feishu_token(agent_id)
    if creds:
        _, token = creds
        data = await _base_api_post(token, f"/bitable/v1/apps/{base_token}/tables/{table_id}/fields", body)
        field = data.get("field", data)
        return _render_base_field_create(table_id, field)

    return _not_configured_error(tn)


async def _feishu_base_record_list(agent_id, arguments: dict) -> str:
    base_token = str(arguments.get("base_token") or "").strip()
    table_id = str(arguments.get("table_id") or "").strip()
    if not base_token:
        return "❌ Missing required argument 'base_token'"
    if not table_id:
        return "❌ Missing required argument 'table_id'"

    limit = min(max(1, int(arguments.get("limit", 100))), 200)
    view_id = str(arguments.get("view_id") or "").strip()
    page_token = str(arguments.get("page_token") or "").strip()
    offset = max(0, int(arguments.get("offset", 0)))
    field_names = _normalize_field_names(arguments.get("field_names") or arguments.get("fields"))
    filter_field = str(arguments.get("filter_field") or "").strip()
    filter_op = str(arguments.get("filter_op") or "").strip()
    filter_value = arguments.get("filter_value")
    has_filter = bool(filter_field and filter_op)
    fetch_all = _truthy(arguments.get("fetch_all")) or has_filter
    max_records = min(max(1, int(arguments.get("max_records", 1000))), 5000)

    creds = await _get_feishu_token(agent_id)
    if creds:
        _, token = creds
        try:
            path = f"/bitable/v1/apps/{base_token}/tables/{table_id}/records"
            if fetch_all:
                collected: list[dict] = []
                next_page = page_token
                total: int | None = None
                has_more = False
                for _ in range(100):
                    remaining = max_records - len(collected)
                    if remaining <= 0:
                        has_more = True
                        break
                    data = await _base_api_get(
                        token,
                        path,
                        _base_record_list_params(
                            page_size=min(200, remaining),
                            view_id=view_id,
                            page_token=next_page,
                        ),
                    )
                    page_items = data.get("items", [])
                    if not isinstance(page_items, list):
                        page_items = []
                    collected.extend(page_items)
                    if isinstance(data.get("total"), int):
                        total = data["total"]
                    has_more = bool(data.get("has_more"))
                    next_page = str(data.get("page_token") or "").strip()
                    if not has_more or not next_page:
                        break

                scanned_items = collected[offset:] if offset else collected
                filtered_items = _filter_records(
                    scanned_items,
                    field_name=filter_field,
                    op=filter_op,
                    expected=filter_value,
                )
                display_items = [_project_record_fields(item, field_names) for item in filtered_items]
                return _render_base_records(
                    table_id,
                    display_items,
                    total=total,
                    has_more=has_more,
                    next_page_token=next_page if has_more and next_page else None,
                    field_names=field_names,
                    scanned_count=len(scanned_items),
                    matched_count=len(filtered_items) if has_filter else None,
                )

            if offset and not page_token:
                collected: list[dict] = []
                consumed = 0
                next_page = ""
                total: int | None = None
                has_more = False
                for _ in range(100):
                    data = await _base_api_get(
                        token,
                        path,
                        _base_record_list_params(page_size=200, view_id=view_id, page_token=next_page),
                    )
                    page_items = data.get("items", [])
                    if not isinstance(page_items, list):
                        page_items = []
                    if isinstance(data.get("total"), int):
                        total = data["total"]
                    page_end = consumed + len(page_items)
                    if page_end > offset and len(collected) < limit:
                        start = max(offset - consumed, 0)
                        remaining = limit - len(collected)
                        collected.extend(page_items[start : start + remaining])
                        if len(collected) >= limit:
                            has_more = start + remaining < len(page_items) or _payload_has_more(
                                data, returned_count=page_end, offset=0
                            )
                            break
                    consumed = page_end
                    has_more = _payload_has_more(data, returned_count=consumed, offset=0)
                    if not has_more:
                        break
                    next_page = str(data.get("page_token") or "").strip()
                    if not next_page:
                        break
                filtered_items = _filter_records(
                    collected[:limit],
                    field_name=filter_field,
                    op=filter_op,
                    expected=filter_value,
                )
                display_items = [_project_record_fields(item, field_names) for item in filtered_items]
                return _render_base_records(
                    table_id,
                    display_items,
                    total=total,
                    has_more=has_more,
                    next_offset=offset + len(collected) if has_more else None,
                    field_names=field_names,
                    scanned_count=len(collected),
                    matched_count=len(filtered_items) if has_filter else None,
                )

            data = await _base_api_get(
                token,
                path,
                _base_record_list_params(page_size=limit, view_id=view_id, page_token=page_token),
            )
            items = data.get("items", [])
            if not isinstance(items, list):
                items = []
            has_more = bool(data.get("has_more")) if page_token else _payload_has_more(
                data, returned_count=len(items), offset=offset
            )
            next_page_token = str(data.get("page_token") or "").strip() if has_more else ""
            filtered_items = _filter_records(
                items,
                field_name=filter_field,
                op=filter_op,
                expected=filter_value,
            )
            display_items = [_project_record_fields(item, field_names) for item in filtered_items]
            return _render_base_records(
                table_id,
                display_items,
                total=data.get("total"),
                has_more=has_more,
                next_page_token=next_page_token or None,
                next_offset=offset + len(items) if has_more and not next_page_token else None,
                field_names=field_names,
                scanned_count=len(items) if has_filter else None,
                matched_count=len(filtered_items) if has_filter else None,
            )
        except Exception as exc:
            logger.warning("[FeishuBase] OpenAPI record_list failed, trying CLI: %s", exc)

    if not await _feishu_cli_available():
        return _not_configured_error("feishu_base_record_list")

    command = [
        "base",
        "+record-list",
        "--base-token",
        base_token,
        "--table-id",
        table_id,
        "--offset",
        str(offset),
        "--limit",
        str(limit),
    ]
    if view_id:
        command.extend(["--view-id", view_id])
    payload = await _run_feishu_base_shortcut(command)
    items = payload.get("items", [])
    if not isinstance(items, list):
        items = []
    filtered_items = _filter_records(items, field_name=filter_field, op=filter_op, expected=filter_value)
    display_items = [_project_record_fields(item, field_names) for item in filtered_items]
    return _render_base_records(
        table_id,
        display_items,
        total=payload.get("total"),
        field_names=field_names,
        scanned_count=len(items) if has_filter else None,
        matched_count=len(filtered_items) if has_filter else None,
    )


async def _feishu_base_record_upsert(agent_id, arguments: dict) -> str:
    base_token = str(arguments.get("base_token") or "").strip()
    table_id = str(arguments.get("table_id") or "").strip()
    fields = arguments.get("fields")
    if not base_token:
        return _render_invalid_input("Missing required argument 'base_token'.", tool_name="feishu_base_record_upsert")
    if not table_id:
        return _render_invalid_input("Missing required argument 'table_id'.", tool_name="feishu_base_record_upsert")
    if not isinstance(fields, dict):
        return _render_invalid_input(
            "Argument 'fields' must be a JSON object.",
            tool_name="feishu_base_record_upsert",
            actionable_hint="Pass a field-name to value mapping, for example {'状态': '进行中'}.",
        )

    record_id = str(arguments.get("record_id") or "").strip()

    creds = await _get_feishu_token(agent_id)
    if creds:
        _, token = creds
        try:
            if record_id:
                data = await _base_api_put(
                    token,
                    f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
                    {"fields": fields},
                )
                rec = data.get("record", data)
                return _render_base_upsert(table_id, {"record": rec, "updated": True})
            else:
                data = await _base_api_post(
                    token,
                    f"/bitable/v1/apps/{base_token}/tables/{table_id}/records",
                    {"fields": fields},
                )
                rec = data.get("record", data)
                return _render_base_upsert(table_id, {"record": rec, "updated": False})
        except Exception as exc:
            logger.warning("[FeishuBase] OpenAPI record_upsert failed, trying CLI: %s", exc)

    if not await _feishu_cli_available():
        return _not_configured_error("feishu_base_record_upsert")

    command = ["base", "+record-upsert", "--base-token", base_token, "--table-id", table_id]
    if record_id:
        command.extend(["--record-id", record_id])
    command.extend(["--json", json.dumps(fields, ensure_ascii=False, separators=(",", ":"))])
    payload = await _run_feishu_base_shortcut(command)
    return _render_base_upsert(table_id, payload)


async def _feishu_base_record_upload_attachment(agent_id, arguments: dict) -> str:
    base_token = str(arguments.get("base_token") or "").strip()
    table_id = str(arguments.get("table_id") or "").strip()
    record_id = str(arguments.get("record_id") or "").strip()
    field_id = str(arguments.get("field_id") or "").strip()
    file_path = str(arguments.get("file_path") or "").strip()
    tn = "feishu_base_record_upload_attachment"
    if not base_token:
        return _render_invalid_input("Missing required argument 'base_token'.", tool_name=tn)
    if not table_id:
        return _render_invalid_input("Missing required argument 'table_id'.", tool_name=tn)
    if not record_id:
        return _render_invalid_input("Missing required argument 'record_id'.", tool_name=tn)
    if not field_id:
        return _render_invalid_input("Missing required argument 'field_id'.", tool_name=tn)
    if not file_path:
        return _render_invalid_input(
            "Missing required argument 'file_path'.",
            tool_name=tn,
            actionable_hint="Pass a workspace-relative file path, for example 'workspace/report.pdf'.",
        )

    try:
        absolute_file = _resolve_workspace_file(agent_id, file_path)
    except ValueError as exc:
        return _render_invalid_input(str(exc), tool_name=tn)
    if not absolute_file.exists():
        return render_tool_error(
            tool_name=tn,
            error_class="not_found",
            message=f"Workspace file not found: {file_path}",
            provider="feishu_openapi",
            retryable=False,
            actionable_hint="Write the file into the agent workspace before uploading it to Feishu Base.",
        )

    display_name = str(arguments.get("name") or "").strip() or absolute_file.name

    # Try OpenAPI: upload media then update record
    creds = await _get_feishu_token(agent_id)
    if creds:
        _, token = creds
        try:
            file_size = absolute_file.stat().st_size
            mime_type = mimetypes.guess_type(str(absolute_file))[0] or "application/octet-stream"
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{FEISHU_API}/drive/v1/medias/upload_all",
                    headers={"Authorization": f"Bearer {token}"},
                    data={
                        "file_name": display_name,
                        "parent_type": "bitable_file",
                        "parent_node": base_token,
                        "size": str(file_size),
                    },
                    files={"file": (display_name, absolute_file.open("rb"), mime_type)},
                )
            upload_data = resp.json()
            if upload_data.get("code") != 0:
                raise RuntimeError(f"Upload failed: {upload_data.get('msg')} (code {upload_data.get('code')})")
            file_token = upload_data.get("data", {}).get("file_token", "")
            if not file_token:
                raise RuntimeError("Upload succeeded but no file_token returned")

            # Update record's attachment field
            await _base_api_put(
                token,
                f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
                {"fields": {field_id: [{"file_token": file_token, "name": display_name}]}},
            )
            return _render_base_attachment_upload(
                table_id,
                {
                    "record": {"record_id": record_id},
                    "attachment": {"file_token": file_token, "name": display_name},
                },
            )
        except Exception as exc:
            logger.warning("[FeishuBase] OpenAPI attachment upload failed, trying CLI: %s", exc)

    # CLI fallback
    if not await _feishu_cli_available():
        return _not_configured_error(tn)

    command = [
        "base",
        "+record-upload-attachment",
        "--base-token",
        base_token,
        "--table-id",
        table_id,
        "--record-id",
        record_id,
        "--field-id",
        field_id,
        "--file",
        str(absolute_file),
    ]
    if display_name != absolute_file.name:
        command.extend(["--name", display_name])
    payload = await _run_feishu_base_shortcut(command)
    return _render_base_attachment_upload(table_id, payload)


async def _feishu_base_record_delete(agent_id, arguments: dict) -> str:
    base_token = str(arguments.get("base_token") or "").strip()
    table_id = str(arguments.get("table_id") or "").strip()
    record_id = str(arguments.get("record_id") or "").strip()
    tn = "feishu_base_record_delete"
    if not base_token:
        return _render_invalid_input("Missing required argument 'base_token'.", tool_name=tn)
    if not table_id:
        return _render_invalid_input("Missing required argument 'table_id'.", tool_name=tn)
    if not record_id:
        return _render_invalid_input("Missing required argument 'record_id'.", tool_name=tn)

    creds = await _get_feishu_token(agent_id)
    if creds:
        _, token = creds
        try:
            await _base_api_delete(
                token,
                f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
            )
            return _render_base_record_delete(table_id, record_id)
        except Exception as exc:
            logger.warning("[FeishuBase] OpenAPI record_delete failed, trying CLI: %s", exc)

    if not await _feishu_cli_available():
        return _not_configured_error(tn)

    payload = await _run_feishu_base_shortcut(
        [
            "base",
            "+record-delete",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--record-id",
            record_id,
        ]
    )
    deleted_record_id = payload.get("record_id") or record_id
    return _render_base_record_delete(table_id, deleted_record_id)
