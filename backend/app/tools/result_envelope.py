"""Structured tool result envelopes for recoverable failures."""

from __future__ import annotations

import json
from typing import Any


def classify_http_status(status_code: int) -> tuple[str, bool]:
    if status_code == 400:
        return "provider_bad_request", False
    if status_code == 401:
        return "auth_or_permission", False
    if status_code == 402:
        return "quota_or_billing", False
    if status_code == 403:
        return "auth_or_permission", False
    if status_code == 404:
        return "not_found", False
    if status_code == 408:
        return "timeout", True
    if status_code == 429:
        return "rate_limited", True
    if 500 <= status_code <= 599:
        return "provider_unavailable", True
    return "provider_error", False


def render_tool_error(
    *,
    tool_name: str,
    error_class: str,
    message: str,
    provider: str | None = None,
    http_status: int | None = None,
    retryable: bool = False,
    actionable_hint: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "ok": False,
        "tool_name": tool_name,
        "error_class": error_class,
        "message": message,
        "provider": provider,
        "http_status": http_status,
        "retryable": retryable,
    }
    if actionable_hint:
        payload["actionable_hint"] = actionable_hint
    if extra:
        payload.update(extra)

    parts = [f"❌ {message}"]
    if actionable_hint:
        parts.append(f"Hint: {actionable_hint}")
    parts.append(f"<tool_error>{json.dumps(payload, ensure_ascii=False)}</tool_error>")
    return "\n\n".join(parts)


def render_tool_fallback(
    *,
    tool_name: str,
    error_class: str,
    message: str,
    fallback_tool: str,
    fallback_result: str,
    provider: str | None = None,
    http_status: int | None = None,
    retryable: bool = False,
    actionable_hint: str | None = None,
) -> str:
    payload_extra = {"fallback_tool": fallback_tool}
    error_block = render_tool_error(
        tool_name=tool_name,
        error_class=error_class,
        message=message,
        provider=provider,
        http_status=http_status,
        retryable=retryable,
        actionable_hint=actionable_hint,
        extra=payload_extra,
    )
    return (
        f"⚠️ {message}\n\n"
        f"Fallback tool used: `{fallback_tool}`\n\n"
        f"{fallback_result}\n\n"
        f"{error_block}"
    )
