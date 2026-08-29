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
    ``terminal_signal``; covered by ``tests/kernel/test_ccplus_side_effects.py``),
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
    return f"⚠️ {message}\n\nFallback tool used: `{fallback_tool}`\n\n{fallback_result}\n\n{error_block}"
