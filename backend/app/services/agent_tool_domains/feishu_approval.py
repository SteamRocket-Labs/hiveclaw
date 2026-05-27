"""Feishu approval tools via OpenAPI."""

from __future__ import annotations

import json
from collections.abc import Iterable
import uuid

from app.services.agent_tool_domains.feishu_helpers import _get_feishu_app_credentials
from app.services.feishu_service import feishu_service
from app.tools.result_envelope import render_tool_error

_ALIAS_STRIP_CHARS = " \t\r\n`'\"“”‘’.,，。:：;；!！?？、()（）[]【】<>《》"


def _approval_not_configured(tool_name: str) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class="not_configured",
        message="Feishu approval auth is not configured for this agent.",
        provider="feishu_openapi",
        actionable_hint="Configure Feishu bot credentials for the agent or tenant first.",
    )


async def _get_approval_credentials(agent_id: uuid.UUID, tool_name: str) -> tuple[str, str] | str:
    creds = await _get_feishu_app_credentials(agent_id)
    if not creds:
        return _approval_not_configured(tool_name)
    return creds


def _load_json_maybe(value):
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return value


def _normalize_alias(value) -> str:
    normalized = str(value or "").strip().strip(_ALIAS_STRIP_CHARS).strip()
    return normalized.lower().replace(" ", "")


def _iter_i18n_pairs(value) -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        key = value.get("key")
        text = value.get("value") or value.get("text") or value.get("name")
        if isinstance(key, str) and key.startswith("@i18n@") and isinstance(text, str):
            yield key, text
        for item_key, item_value in value.items():
            if isinstance(item_key, str) and item_key.startswith("@i18n@") and isinstance(item_value, str):
                yield item_key, item_value
            else:
                yield from _iter_i18n_pairs(item_value)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_i18n_pairs(item)


def _extract_i18n_map(definition: dict) -> dict[str, str]:
    return dict(_iter_i18n_pairs(definition))


def _parse_widget_list(value) -> list[dict]:
    value = _load_json_maybe(value)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        if isinstance(value.get("id"), str):
            return [value]
        for key in ("form_content", "widgets", "widget_list", "items", "children"):
            if key in value:
                parsed = _parse_widget_list(value[key])
                if parsed:
                    return parsed
    return []


def _extract_approval_widgets(definition: dict) -> list[dict]:
    for candidate in (
        definition.get("form"),
        definition.get("form_content"),
        definition.get("approval", {}).get("form") if isinstance(definition.get("approval"), dict) else None,
        definition.get("approval", {}).get("form_content") if isinstance(definition.get("approval"), dict) else None,
    ):
        widgets = _parse_widget_list(candidate)
        if widgets:
            return widgets
    return []


def _widget_aliases(widget: dict, i18n_map: dict[str, str]) -> set[str]:
    aliases: set[str] = set()
    for key in ("id", "custom_id", "name", "title", "field_name"):
        value = widget.get(key)
        if not isinstance(value, str):
            continue
        aliases.add(_normalize_alias(value))
        if value in i18n_map:
            aliases.add(_normalize_alias(i18n_map[value]))
    return {alias for alias in aliases if alias}


def _build_widget_lookup(definition: dict) -> dict[str, dict]:
    i18n_map = _extract_i18n_map(definition)
    lookup: dict[str, dict] = {}
    for widget in _extract_approval_widgets(definition):
        for alias in _widget_aliases(widget, i18n_map):
            lookup.setdefault(alias, widget)
    return lookup


def _approval_form_entry(widget: dict, value) -> dict:
    entry = {"id": widget["id"], "type": widget.get("type") or "input", "value": value}
    return entry


def _normalize_approval_form_with_definition(form, definition: dict) -> tuple[str | None, str | None]:
    form_value = _load_json_maybe(form)
    widgets = _extract_approval_widgets(definition)
    lookup = _build_widget_lookup(definition)
    if not widgets or not lookup:
        return None, "未能从飞书审批定义中解析到表单控件。请确认应用有读取审批定义权限。"

    normalized: list[dict] = []
    unmatched: list[str] = []

    if isinstance(form_value, dict) and "id" in form_value and "value" in form_value:
        form_value = [form_value]

    if isinstance(form_value, dict):
        for field_name, field_value in form_value.items():
            widget = lookup.get(_normalize_alias(field_name))
            if not widget:
                unmatched.append(str(field_name))
                continue
            normalized.append(_approval_form_entry(widget, field_value))
    elif isinstance(form_value, list):
        for item in form_value:
            if not isinstance(item, dict):
                unmatched.append(str(item))
                continue
            lookup_value = item.get("id") or item.get("custom_id") or item.get("name")
            widget = lookup.get(_normalize_alias(lookup_value))
            if not widget:
                unmatched.append(str(lookup_value or item))
                continue
            normalized.append(_approval_form_entry(widget, item.get("value")))
    else:
        return None, "form 必须是字段名到值的对象，或包含 id/name/value 的数组。"

    if unmatched:
        available = [
            str(widget.get("name") or widget.get("custom_id") or widget.get("id"))
            for widget in widgets
            if widget.get("id")
        ]
        return None, (
            "审批表单字段无法匹配到真实控件 ID："
            f"{', '.join(unmatched)}。可用字段：{', '.join(available) or '无'}"
        )

    return json.dumps(normalized, ensure_ascii=False), None


async def _prepare_approval_form(app_id: str, app_secret: str, approval_code: str, form) -> str | tuple[None, str]:
    form_value = _load_json_maybe(form)
    needs_definition = isinstance(form_value, dict)
    if isinstance(form_value, list):
        needs_definition = any(
            isinstance(item, dict) and _normalize_alias(item.get("id") or item.get("name") or item.get("custom_id"))
            for item in form_value
        )

    if not needs_definition:
        return json.dumps(form_value, ensure_ascii=False) if not isinstance(form, str) else form

    try:
        definition = await feishu_service.get_approval_definition(app_id, app_secret, approval_code)
    except Exception as exc:
        if isinstance(form_value, list):
            return json.dumps(form_value, ensure_ascii=False) if not isinstance(form, str) else form
        return None, f"读取飞书审批定义失败，无法把字段名映射成控件 ID：{exc}"

    normalized, error = _normalize_approval_form_with_definition(form_value, definition)
    if error:
        return None, error
    return normalized or (json.dumps(form_value, ensure_ascii=False) if not isinstance(form, str) else form)


def _render_approval_definition(approval_code: str, definition: dict) -> str:
    i18n_map = _extract_i18n_map(definition)
    widgets = _extract_approval_widgets(definition)
    lines = [f"📐 **Feishu approval definition** (`{approval_code}`)"]
    if not widgets:
        lines.append("未解析到表单控件。")
        return "\n".join(lines)
    for widget in widgets:
        raw_name = str(widget.get("name") or "")
        name = i18n_map.get(raw_name, raw_name) or str(widget.get("custom_id") or widget.get("id"))
        required = " required" if widget.get("required") else ""
        custom_id = f", custom_id=`{widget.get('custom_id')}`" if widget.get("custom_id") else ""
        lines.append(f"- `{widget.get('id')}` ({widget.get('type') or 'unknown'}{required}{custom_id}): {name}")
    return "\n".join(lines)


async def _feishu_approval_create(agent_id: uuid.UUID, arguments: dict) -> str:
    approval_code = str(arguments.get("approval_code") or "").strip()
    user_id = str(arguments.get("user_id") or "").strip()
    form = arguments.get("form")
    if not approval_code:
        return "❌ Missing required argument 'approval_code'"
    if not user_id:
        return "❌ Missing required argument 'user_id'"
    if form is None:
        return "❌ Missing required argument 'form'"

    creds = await _get_approval_credentials(agent_id, "feishu_approval_create")
    if isinstance(creds, str):
        return creds
    app_id, app_secret = creds
    prepared_form = await _prepare_approval_form(app_id, app_secret, approval_code, form)
    if isinstance(prepared_form, tuple):
        return render_tool_error(
            tool_name="feishu_approval_create",
            error_class="invalid_approval_form",
            message=prepared_form[1],
            provider="feishu_openapi",
            actionable_hint="先调用 feishu_approval_definition 查看字段，或直接用字段中文名和值组成对象提交。",
        )
    payload = await feishu_service.create_approval_instance(
        app_id,
        app_secret,
        approval_code,
        user_id,
        prepared_form,
    )
    instance = payload.get("instance_code") or payload.get("instance_id") or payload.get("instance", {}).get("instance_code", "")
    lines = ["✅ 已创建飞书审批实例"]
    if instance:
        lines.append(f"- Instance ID: `{instance}`")
    lines.append(f"- Approval Code: `{approval_code}`")
    lines.append(f"- User ID: `{user_id}`")
    return "\n".join(lines)


async def _feishu_approval_query(agent_id: uuid.UUID, arguments: dict) -> str:
    approval_code = str(arguments.get("approval_code") or "").strip()
    status = str(arguments.get("status") or "").strip() or None
    if not approval_code:
        return "❌ Missing required argument 'approval_code'"

    creds = await _get_approval_credentials(agent_id, "feishu_approval_query")
    if isinstance(creds, str):
        return creds
    app_id, app_secret = creds
    payload = await feishu_service.query_approval_instances(app_id, app_secret, approval_code, status=status)
    instances = (
        payload.get("instance_code_list")
        or payload.get("instance_id_list")
        or payload.get("instance_list")
        or []
    )
    lines = [f"📋 **Feishu approval instances** (`{approval_code}`)"]
    if not instances:
        lines.append("当前没有匹配的审批实例。")
        return "\n".join(lines)
    for item in instances:
        if isinstance(item, dict):
            instance_id = item.get("instance_code") or item.get("instance_id") or json.dumps(item, ensure_ascii=False)
        else:
            instance_id = str(item)
        lines.append(f"- `{instance_id}`")
    return "\n".join(lines)


async def _feishu_approval_definition(agent_id: uuid.UUID, arguments: dict) -> str:
    approval_code = str(arguments.get("approval_code") or "").strip()
    if not approval_code:
        return "❌ Missing required argument 'approval_code'"

    creds = await _get_approval_credentials(agent_id, "feishu_approval_definition")
    if isinstance(creds, str):
        return creds
    app_id, app_secret = creds
    payload = await feishu_service.get_approval_definition(app_id, app_secret, approval_code)
    return _render_approval_definition(approval_code, payload)


async def _feishu_approval_get(agent_id: uuid.UUID, arguments: dict) -> str:
    instance_id = str(arguments.get("instance_id") or "").strip()
    if not instance_id:
        return "❌ Missing required argument 'instance_id'"

    creds = await _get_approval_credentials(agent_id, "feishu_approval_get")
    if isinstance(creds, str):
        return creds
    app_id, app_secret = creds
    payload = await feishu_service.get_approval_instance(app_id, app_secret, instance_id)
    return (
        f"📄 **Feishu approval instance** `{instance_id}`\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )
