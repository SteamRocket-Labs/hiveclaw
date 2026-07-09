"""Provider-call prompt projection and token ledger helpers."""

from __future__ import annotations

import json
import uuid
from typing import Any


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or "")
    return str(getattr(message, "role", "") or "")


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", None)


def _surface_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") == "image_url":
                    parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
                else:
                    parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def estimate_prompt_tokens_from_text(text: str) -> int:
    """Conservative cheap token estimate used before provider usage exists."""

    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def estimate_surface_tokens(value: Any) -> int:
    return estimate_prompt_tokens_from_text(_surface_text(value))


def _category(
    name: str,
    *,
    text: str = "",
    payload: Any = None,
    item_count: int = 0,
    cacheability: str = "unknown",
) -> dict[str, Any]:
    surface = text if text else _surface_text(payload)
    return {
        "name": name,
        "tokens": estimate_prompt_tokens_from_text(surface),
        "chars": len(surface),
        "item_count": item_count,
        "cacheability": cacheability,
    }


def _tool_schema_text(tools: list[dict[str, Any]] | None) -> str:
    if not tools:
        return ""
    return json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _extra_surface_text(extra_surfaces: dict[str, Any] | None, key: str) -> str:
    value = (extra_surfaces or {}).get(key)
    if isinstance(value, list):
        return "\n\n".join(_surface_text(item) for item in value if _surface_text(item))
    return _surface_text(value)


def build_provider_prompt_ledger(
    *,
    messages: list[Any] | None,
    tools: list[dict[str, Any]] | None = None,
    extra_surfaces: dict[str, Any] | None = None,
    provider: str = "",
    model: str = "",
    round_index: int = 0,
    model_window_tokens: int | None = None,
    provider_call_id: str | None = None,
    cache_hints_applied: bool = False,
) -> dict[str, Any]:
    """Build the provider-call prompt ledger from the exact call surface.

    This ledger is intentionally provider-agnostic. It measures the local
    request projection before provider usage is available, including `tools=`
    schemas that are not part of the message list.
    """

    prompt_messages = list(messages or [])
    system_text = "\n\n".join(
        _surface_text(_message_content(message)) for message in prompt_messages if _message_role(message) == "system"
    )
    non_system_payload = [
        {
            "role": _message_role(message),
            "content": _message_content(message),
        }
        for message in prompt_messages
        if _message_role(message) != "system"
    ]
    dynamic_notice = _extra_surface_text(extra_surfaces, "dynamic_notice")
    runtime_reminders = _extra_surface_text(extra_surfaces, "runtime_reminders")
    vision_payloads = _extra_surface_text(extra_surfaces, "vision_payloads")
    tool_schema_text = _tool_schema_text(tools)

    categories = [
        _category(
            "system_prompt",
            text=system_text,
            item_count=sum(1 for message in prompt_messages if _message_role(message) == "system"),
            cacheability="frozen",
        ),
        _category("messages", payload=non_system_payload, item_count=len(non_system_payload), cacheability="transcript"),
        _category("dynamic_notice", text=dynamic_notice, item_count=1 if dynamic_notice else 0, cacheability="volatile"),
        _category(
            "runtime_reminders",
            text=runtime_reminders,
            item_count=len((extra_surfaces or {}).get("runtime_reminders") or [])
            if isinstance((extra_surfaces or {}).get("runtime_reminders"), list)
            else (1 if runtime_reminders else 0),
            cacheability="volatile",
        ),
        _category(
            "tool_schemas",
            text=tool_schema_text,
            item_count=len(tools or []),
            cacheability="provider_tool_schema",
        ),
        _category("vision_payloads", text=vision_payloads, item_count=1 if vision_payloads else 0, cacheability="volatile"),
    ]
    projected_input_tokens = sum(int(item["tokens"]) for item in categories)
    projected_uncached_input_tokens = sum(
        int(item["tokens"]) for item in categories if item.get("cacheability") != "frozen"
    )
    tool_schema_tokens = next(item["tokens"] for item in categories if item["name"] == "tool_schemas")

    return {
        "schema": "hive.ccplus.provider_prompt_ledger.v1",
        "provider_call_id": provider_call_id or f"provider-call-{uuid.uuid4().hex}",
        "round": max(0, int(round_index or 0)),
        "provider": provider,
        "model": model,
        "model_window_tokens": model_window_tokens,
        "projected_input_tokens": projected_input_tokens,
        "projected_uncached_input_tokens": projected_uncached_input_tokens,
        "tool_schema_tokens": int(tool_schema_tokens),
        "cache_hints_applied": bool(cache_hints_applied),
        "categories": categories,
    }


def prompt_projection_token_count(
    *,
    messages: list[Any] | None,
    tools: list[dict[str, Any]] | None = None,
    extra_surfaces: dict[str, Any] | None = None,
) -> int:
    ledger = build_provider_prompt_ledger(messages=messages, tools=tools, extra_surfaces=extra_surfaces)
    return int(ledger["projected_input_tokens"])
