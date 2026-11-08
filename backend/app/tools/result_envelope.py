"""Structured tool result envelopes: recoverable-failure renders + typed
multimodal content (text + image/document blocks)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolResultBlock:
    """Provider-neutral content block inside a tool result."""

    type: str  # "text" | "image" | "document"
    text: str | None = None
    media_type: str | None = None  # e.g. "image/png", "application/pdf"
    data: str | None = None  # base64-encoded bytes (for image / document)


@dataclass(frozen=True)
class ToolContentEnvelope:
    """A tool result carrying typed content blocks plus a plain-text fallback.

    ``text`` is the always-present evidence rendering used by str-assuming audit,
    UI, and compatibility paths. ``model_visible_text`` may provide a truthful,
    bounded provider projection when the complete receipt contains navigation or
    idempotency fields that the model does not need; the kernel preserves ``text``
    in the durable tool event and sends only that explicit projection to the next
    model round. ``blocks`` carry typed content mapped per-provider at the
    llm_client adapter layer — L3 model equality means best-effort per provider
    (Anthropic supports image/document tool_result; OpenAI/Gemini fall back to
    the projected text). ``__str__`` still returns the complete ``text`` so
    existing audit and compatibility paths keep working untouched.

    DEFERRED CONTRACT — ``new_messages`` and ``terminal_signal`` are a tested
    forward extension point with NO production producer yet. The kernel
    *consumes* them (``AgentKernel._extract_tool_side_effects`` injects
    ``new_messages`` into the live conversation and ends the turn on
    ``terminal_signal``; the outer runtime preserves that typed signal and
    finalizes without fabricating an assistant message),
    but every live terminal/clarification flow today is driven by the JSON-status
    marker path (``_tool_result_requests_user_clarification`` →
    ``awaiting_user_clarification`` / ``plan_mode_entry_requested``), so these two
    fields stay empty in practice. They activate when a tool needs to inject
    follow-up messages or hard-terminate a turn structurally rather than via a
    JSON marker; until then this is an explicitly-tracked deferral, not a silent
    seeded-but-unwired field. ``test_side_effect_channel_has_no_production_producer``
    fails if a producer appears without this note being revisited.
    """

    text: str
    model_visible_text: str | None = None
    blocks: tuple[ToolResultBlock, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    # Deferred side-effect channel (see class docstring): consumed by the kernel,
    # no production producer yet — terminal/clarification flows use JSON markers.
    new_messages: tuple[dict[str, Any], ...] = ()
    context_modifier: dict[str, Any] | None = None
    artifacts: tuple[dict[str, Any], ...] = ()
    t0_refs: tuple[str, ...] = ()
    invocation_span_id: str | None = None
    mcp_meta: dict[str, Any] | None = None
    permission_request: dict[str, Any] | None = None
    terminal_signal: str | None = None

    def __str__(self) -> str:
        return self.text

    def __contains__(self, item: str) -> bool:
        return item in self.text

    def lower(self) -> str:
        return self.text.lower()

    @classmethod
    def image(cls, *, text: str, media_type: str, data: str) -> "ToolContentEnvelope":
        return cls(text=text, blocks=(ToolResultBlock(type="image", media_type=media_type, data=data),))

    @classmethod
    def document(cls, *, text: str, media_type: str, data: str) -> "ToolContentEnvelope":
        return cls(text=text, blocks=(ToolResultBlock(type="document", media_type=media_type, data=data),))


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


_ERROR_COPY_GROUPS = (
    (("bad_arguments", "invalid_arguments", "invalid_input", "invalid_tool_arguments", "invalid_approval_form", "provider_bad_request", "wrong_tool"),
        "这次操作缺少必要信息，或提供的信息不正确。", "补充或更正信息后再试。"),
    (("auth_or_permission", "auth_required", "forbidden", "authz_policy_violation", "unauthorized_secret_bytes", "plan_mode_readonly_violation"),
        "当前没有权限完成这次操作。", "请使用你有权访问的资料，或先申请相应授权。"),
    (("authority_context_unavailable", "missing_handoff_authority"),
        "暂时无法确认这次操作所需的授权。", "请稍后重试，或改用已获授权的资料。"),
    (("approval_required",), "这次操作需要你确认后才能继续。", "请先确认或拒绝这次操作。"),
    (("approval_asset_revision_drift", "approval_payload_mutation", "approval_hook_block"),
        "这次操作的内容已变化，需要重新确认。", "请查看最新内容后再确认。"),
    (("quota_or_billing",), "可用额度不足，操作未完成。", "补充额度或调整用量后再试。"),
    (("rate_limited",), "请求过于频繁，操作未完成。", "稍等片刻后再试。"),
    (("not_found",), "没有找到要使用的内容。", "请检查名称或位置后再试。"),
    (("timeout",), "操作等待超时，结果未知。", "先查看原操作的状态或已有结果，再决定是否重试。"),
    (("cancelled",), "操作已停止。", "如需继续，请重新发起请求。"),
    (("configuration_required", "not_configured", "provider_not_configured", "extension_disabled"),
        "这项功能还没有连接好。", "请先完成连接设置，再继续操作。"),
    (("provider_unavailable", "backend_unavailable", "transport_failure", "governance_dependency_unavailable", "runtime_budget_service_unavailable", "approval_ticket_unavailable"),
        "所需服务暂时不可用，操作未完成。", "请稍后再试。"),
    (("empty_content", "provider_empty_response"), "没有收到可用内容。", "请补充内容或稍后再试。"),
    (("incomplete_content", "provider_bad_response"), "收到的内容不完整，操作未完成。", "请重新获取完整内容后再试。"),
    (("unsupported_file_type", "unreadable_pdf"), "暂时无法读取这种文件。", "请换用可读取的文件格式。"),
    (("bad_state",), "当前状态不支持这次操作。", "请查看当前进度后再决定下一步。"),
)
_ERROR_COPY = {name: (message, hint) for names, message, hint in _ERROR_COPY_GROUPS for name in names}


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
    legacy_format: bool = False,
) -> str:
    """Render current copy, or reconstruct immutable historical evidence bytes."""
    diagnostic_message, diagnostic_hint = message, actionable_hint
    if not legacy_format:
        message, actionable_hint = _ERROR_COPY.get(
            error_class, ("操作未能完成，结果尚未确认。", "请查看已有结果后再决定下一步。")
        )
        if error_class == "timeout" and extra and extra.get("timeout_seconds") is not None:
            message = f"操作超过 {int(extra['timeout_seconds'])} 秒没有完成，结果未知。"
    payload: dict[str, Any] = {
        "ok": False,
        "tool_name": tool_name,
        "error_class": error_class,
        "message": message,
        "provider": provider,
        "http_status": http_status,
        "retryable": retryable,
    }
    if not legacy_format:
        payload["diagnostic_message"] = diagnostic_message
        if diagnostic_hint:
            payload["diagnostic_hint"] = diagnostic_hint
    if actionable_hint:
        payload["actionable_hint"] = actionable_hint
    if extra:
        payload.update(extra)

    parts = [f"❌ {message}"]
    if actionable_hint:
        parts.append(f"Hint: {actionable_hint}" if legacy_format else actionable_hint)
    parts.append(f"<tool_error>{json.dumps(payload, ensure_ascii=False)}</tool_error>")
    return "\n\n".join(parts)


def render_tool_fallback(
    *,
    tool_name: str,
    error_class: str,
    message: str,
    fallback_tool: str,
    fallback_result: str | ToolContentEnvelope,
    provider: str | None = None,
    http_status: int | None = None,
    retryable: bool = False,
    actionable_hint: str | None = None,
) -> str | ToolContentEnvelope:
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
    from app.tools.service import _tool_result_failed

    if _tool_result_failed(fallback_result) or str(fallback_result).lstrip().startswith("❌"):
        return f"{error_block}\n\n{fallback_result}"
    diagnostic = json.loads(error_block.split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
    return ToolContentEnvelope(
        text=f"已改用其他方式获取以下结果：\n\n{fallback_result}",
        metadata={**getattr(fallback_result, "metadata", {}), "primary_tool_error": diagnostic},
    )
