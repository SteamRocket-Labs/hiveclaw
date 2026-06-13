"""Provider-agnostic prompt caching layer.

Design principle: **capability-driven, not name-driven**.

This module does NOT hardcode provider names. Instead it uses two
mechanisms that work for ANY LLM provider, present or future:

1. **Hint injection**: a boolean `supports_cache_control` flag decides
   whether to inject explicit `cache_control` markers. The flag
   defaults to auto-detection but can be overridden per model config.
   Any unknown provider gets NO markers (safe default — most providers
   either cache automatically or don't cache at all).

2. **Metrics extraction**: a universal field probe that tries ALL known
   cache metric field names from any provider. If a new provider uses
   `cached_tokens` or `cache_read_input_tokens` or any other known field,
   it is picked up automatically — zero code change needed.

The application-level cache (frozen prefix reuse in SessionContext)
benefits ALL providers regardless of API-level support.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── Prompt cache boundary marker ────────────────────────────────
# Shared with prompt_builder.py — used to split system prompt into
# frozen (cacheable) + dynamic (volatile) portions.
PROMPT_CACHE_BOUNDARY = "__PROMPT_DYNAMIC_BOUNDARY__"


# ── Cache metrics (provider-agnostic) ───────────────────────────

@dataclass(slots=True)
class CacheMetrics:
    """Structured cache observation extracted from an LLM response."""

    provider: str = ""
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    uncached_input_tokens: int = 0
    total_input_tokens: int = 0
    cache_hit: bool = False

    @property
    def hit_rate(self) -> float:
        if self.total_input_tokens == 0:
            return 0.0
        return self.cache_read_tokens / self.total_input_tokens

    def as_log_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "total_input_tokens": self.total_input_tokens,
            "cache_hit": self.cache_hit,
            "cache_hit_rate": round(self.hit_rate, 3),
        }


# ── Capability detection ────────────────────────────────────────
# Only ONE capability matters for hint injection: does the provider accept
# explicit `cache_control` content blocks? Capability metadata is the source
# of truth; unknown providers default to safe passthrough.

def _supports_cache_control(provider: str) -> bool:
    """Does this provider's API accept cache_control markers in content blocks?

    Reads the `supports_cache_control` flag from ProviderSpec (PROVIDER_REGISTRY).
    Returns False for any unknown provider — safe default because most providers
    use automatic prefix caching and don't need explicit markers.
    """
    try:
        from app.services.llm_client import get_provider_spec

        spec = get_provider_spec(provider)
        if spec is not None:
            return spec.supports_cache_control
    except Exception:
        pass
    return False


# ── Hint injection ──────────────────────────────────────────────

def apply_cache_hints(
    messages: list,
    provider: str,
    *,
    invocation_scope: str = "conversation",
    supports_cache_control_override: bool | None = None,
) -> list:
    """Inject cache hints into LLM messages if the provider supports them.

    This is capability-driven: unknown providers get passthrough (safe).
    The `supports_cache_control_override` parameter allows per-model opt-in
    without touching this module (e.g., if a new provider adopts explicit
    cache-control blocks, set override=True in model config).

    Args:
        messages: LLMMessage list (system + user/assistant history)
        provider: raw provider string from model config
        invocation_scope: "conversation" | "heartbeat" | "trigger" | "task"
        supports_cache_control_override: explicit opt-in/out (None = auto-detect)
    """
    use_cache_control = (
        supports_cache_control_override
        if supports_cache_control_override is not None
        else _supports_cache_control(provider)
    )

    if use_cache_control:
        return _apply_cache_control_hints(messages, invocation_scope=invocation_scope)

    # All other providers: passthrough. They benefit from the application-
    # level frozen prefix stability (Phase 1a-1c) without needing API markers.
    return messages


def _apply_cache_control_hints(
    messages: list,
    *,
    invocation_scope: str = "conversation",
) -> list:
    """Apply explicit cache_control content-block hints.

    TTL strategy:
    - conversation: 5-min default (frequent requests keep the sliding window
      alive; cheaper write cost at 1.25x base)
    - heartbeat / trigger / task: 1-hour TTL (autonomous modes have 15-45 min
      gaps between calls; 5-min TTL would always expire; 1h costs 2.0x write
      but 0.1x reads — pays off after 2 cache hits)
    """
    if invocation_scope in ("heartbeat", "trigger", "task"):
        cache_control = {"type": "ephemeral", "ttl": "1h"}
    else:
        cache_control = {"type": "ephemeral"}

    result = list(messages)

    # Split system message at cache boundary into frozen (cached) + dynamic
    for i, msg in enumerate(result):
        if msg.role == "system" and msg.content and isinstance(msg.content, str):
            if PROMPT_CACHE_BOUNDARY.strip() in msg.content:
                parts = msg.content.split(PROMPT_CACHE_BOUNDARY.strip(), 1)
                frozen_text = parts[0].strip()
                dynamic_text = parts[1].strip() if len(parts) > 1 else ""
                blocks: list[dict] = [
                    {"type": "text", "text": frozen_text, "cache_control": cache_control},
                ]
                if dynamic_text:
                    blocks.append({"type": "text", "text": dynamic_text})
                result[i] = _clone_msg(msg, content=blocks)
            else:
                result[i] = _clone_msg(
                    msg,
                    content=[{"type": "text", "text": msg.content, "cache_control": cache_control}],
                )
            break

    # Mark last 3 non-system messages with cache_control (turn-level caching)
    non_system_indices = [i for i, m in enumerate(result) if m.role != "system"]
    for idx in non_system_indices[-3:]:
        msg = result[idx]
        if msg.content and isinstance(msg.content, str):
            result[idx] = _clone_msg(
                msg,
                content=[{"type": "text", "text": msg.content, "cache_control": cache_control}],
            )

    return result


def _clone_msg(msg, **overrides):
    """Clone an LLMMessage with field overrides."""
    from app.services.llm_client import LLMMessage

    return LLMMessage(
        role=overrides.get("role", msg.role),
        content=overrides.get("content", msg.content),
        tool_calls=overrides.get("tool_calls", msg.tool_calls),
        tool_call_id=overrides.get("tool_call_id", msg.tool_call_id),
        reasoning_content=overrides.get("reasoning_content", msg.reasoning_content),
        reasoning_signature=overrides.get("reasoning_signature", msg.reasoning_signature),
    )


# ── Metrics extraction (universal field probe) ──────────────────
#
# Instead of routing by provider name, we probe ALL known field names
# from every provider we've ever seen. A new provider that uses ANY
# of these field names gets metrics extracted automatically.
#
# Known cache-related usage fields (as of 2026-04):
#
# Cache read:
#   cache_read_input_tokens                  — Anthropic
#   prompt_tokens_details.cached_tokens      — OpenAI, Zhipu GLM, MiniMax (OAI-compat), Kimi
#   cached_tokens (top-level)                — Kimi (some models)
#   prompt_cache_hit_tokens                  — DeepSeek
#   cachedContentTokenCount                  — Gemini
#
# Cache write:
#   cache_creation_input_tokens              — Anthropic, MiniMax (Anthropic-compat)
#   prompt_tokens_details.cache_creation_input_tokens — Qwen/DashScope
#
# Total input:
#   input_tokens                             — Anthropic, generic
#   prompt_tokens                            — OpenAI, DeepSeek, generic
#   promptTokenCount                         — Gemini
#
# Cache miss (DeepSeek-specific):
#   prompt_cache_miss_tokens                 — DeepSeek

def _probe_int(usage: dict, *keys: str) -> int:
    """Try multiple keys in order, return the first non-zero int found."""
    for key in keys:
        val = usage.get(key)
        if val is not None and val != 0:
            try:
                return int(val)
            except (ValueError, TypeError):
                continue
    return 0


def extract_cache_metrics(usage: dict | None, provider: str = "") -> CacheMetrics:
    """Extract cache metrics from ANY provider's response usage object.

    Uses universal field probing — no provider-name routing. If a new
    provider reports cache stats using any known field name, it is
    picked up automatically.
    """
    if not usage:
        return CacheMetrics(provider=provider)

    metrics = CacheMetrics(provider=provider)

    # ── Cache read tokens (from any source) ──
    # Merge nested prompt_tokens_details into flat dict so both paths
    # are probed in one pass.
    details = usage.get("prompt_tokens_details") or {}
    merged = {**usage, **details}
    metrics.cache_read_tokens = _probe_int(
        merged,
        "cache_read_input_tokens",     # Anthropic, MiniMax (Anthropic-compat)
        "cached_tokens",               # OpenAI, Zhipu GLM, MiniMax (OAI), Kimi
        "prompt_cache_hit_tokens",     # DeepSeek
        "cachedContentTokenCount",     # Gemini
    )

    # ── Cache write tokens ──
    metrics.cache_write_tokens = _probe_int(
        merged,
        "cache_creation_input_tokens",  # Anthropic, MiniMax, Qwen (may be nested)
    )

    # ── Total input tokens ──
    metrics.total_input_tokens = _probe_int(
        usage,
        "input_tokens",        # Anthropic, generic
        "prompt_tokens",       # OpenAI, DeepSeek, generic
        "promptTokenCount",    # Gemini
    )
    # DeepSeek special: total = hit + miss
    if metrics.total_input_tokens == 0:
        miss = _probe_int(usage, "prompt_cache_miss_tokens")
        if miss or metrics.cache_read_tokens:
            metrics.total_input_tokens = metrics.cache_read_tokens + miss

    # ── Derived fields ──
    metrics.uncached_input_tokens = max(0, metrics.total_input_tokens - metrics.cache_read_tokens)
    metrics.cache_hit = metrics.cache_read_tokens > 0

    return metrics
