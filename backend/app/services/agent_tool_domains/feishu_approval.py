"""Feishu approval tools via OpenAPI."""

from __future__ import annotations

import json
import uuid

from app.services.agent_tool_domains.feishu_helpers import _get_feishu_app_credentials
from app.services.feishu_service import feishu_service
from app.tools.result_envelope import render_tool_error


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
    payload = await feishu_service.create_approval_instance(
        app_id,
        app_secret,
        approval_code,
        user_id,
        json.dumps(form, ensure_ascii=False) if not isinstance(form, str) else form,
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
