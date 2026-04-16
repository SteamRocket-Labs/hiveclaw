"""Provider-agnostic prompt caching layer.

Hive serves multiple LLM providers (Anthropic, OpenAI, DeepSeek, Gemini,
MiniMax, Zhipu, etc.). Each provider has its own caching mechanism:

- Anthropic: explicit `cache_control` markers on content blocks + TTL
- OpenAI / DeepSeek: automatic prefix caching (no markers needed)
- Gemini: explicit Context Caching API (separate endpoint)
- Others: no API-level caching, benefit from application-level only

This module provides a unified interface so the kernel does not need to
know which provider it is talking to. The application-level cache (frozen
prefix reuse in SessionContext) benefits ALL providers; the API-level
hints here are provider-specific sugar on top.
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


# ── Provider detection ──────────────────────────────────────────

def _detect_provider(provider_str: str) -> str:
    """Normalize raw provider string to canonical name."""
    p = (provider_str or "").lower().strip()
    if "anthropic" in p or "claude" in p:
        return "anthropic"
    if "openai" in p or "gpt" in p:
        return "openai"
    if "deepseek" in p:
        return "deepseek"
    if "gemini" in p or "google" in p:
        return "gemini"
    if "minimax" in p:
        return "minimax"
    if "zhipu" in p or "glm" in p:
        return "zhipu"
    return "unknown"


# ── Hint injection (per provider) ───────────────────────────────

def apply_cache_hints(
    messages: list,
    provider: str,
    *,
    execution_mode: str = "conversation",
) -> list:
    """Inject provider-specific cache control hints into LLM messages.

    This is the unified entry point replacing the old Anthropic-only
    `apply_prompt_cache_hints`. The kernel calls this once per LLM round;
    it routes to the appropriate provider strategy.

    Args:
        messages: LLMMessage list (system + user/assistant history)
        provider: raw provider string (e.g. "anthropic", "openai/gpt-4.1")
        execution_mode: "conversation" | "heartbeat" | "trigger" | "task"
            Autonomous modes use longer cache TTL where supported.
    """
    canonical = _detect_provider(provider)

    if canonical == "anthropic":
        return _apply_anthropic_hints(messages, execution_mode=execution_mode)
    if canonical == "openai" or canonical == "deepseek":
        # Automatic prefix caching — no markers needed.
        # The frozen prefix stability from Phase 1a is sufficient.
        return messages
    if canonical == "gemini":
        # Gemini Context Caching uses a separate API call (CreateCachedContent)
        # which must happen before the chat request. This function only handles
        # in-message hints, so Gemini caching is a no-op here. Future: add a
        # pre-flight hook in the kernel for Gemini cached content creation.
        return messages

    # Unknown / unsupported providers — return unchanged.
    return messages


def _apply_anthropic_hints(
    messages: list,
    *,
    execution_mode: str = "conversation",
) -> list:
    """Anthropic-specific: split system prompt at boundary, add cache_control.

    TTL strategy:
    - conversation: 5-min default (frequent requests keep the sliding window
      alive; cheaper write cost at 1.25x base)
    - heartbeat / trigger / task: 1-hour TTL (autonomous modes have 15-45 min
      gaps between calls; 5-min TTL would always expire; 1h costs 2.0x write
      but 0.1x reads — pays off after 2 cache hits)
    """
    # Pick TTL based on execution mode
    if execution_mode in ("heartbeat", "trigger", "task"):
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
                # No boundary marker — cache entire system message
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
    )


# ── Metrics extraction (per provider) ───────────────────────────

def extract_cache_metrics(usage: dict | None, provider: str) -> CacheMetrics:
    """Extract cache metrics from an LLM response's usage object.

    Each provider reports cache stats differently. This normalizes them
    into a unified CacheMetrics structure for observability.
    """
    if not usage:
        return CacheMetrics(provider=_detect_provider(provider))

    canonical = _detect_provider(provider)
    metrics = CacheMetrics(provider=canonical)

    if canonical == "anthropic":
        # Anthropic reports: cache_creation_input_tokens, cache_read_input_tokens
        # Newer: ephemeral_5m_input_tokens, ephemeral_1h_input_tokens
        metrics.cache_write_tokens = int(usage.get("cache_creation_input_tokens", 0) or 0)
        metrics.cache_read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
        metrics.uncached_input_tokens = int(usage.get("input_tokens", 0) or 0) - metrics.cache_read_tokens
        metrics.total_input_tokens = int(usage.get("input_tokens", 0) or 0)
        metrics.cache_hit = metrics.cache_read_tokens > 0

    elif canonical == "openai":
        # OpenAI reports: prompt_tokens_details.cached_tokens
        details = usage.get("prompt_tokens_details") or {}
        metrics.cache_read_tokens = int(details.get("cached_tokens", 0) or 0)
        metrics.total_input_tokens = int(usage.get("prompt_tokens", 0) or 0)
        metrics.uncached_input_tokens = metrics.total_input_tokens - metrics.cache_read_tokens
        metrics.cache_hit = metrics.cache_read_tokens > 0

    elif canonical == "deepseek":
        # DeepSeek reports: prompt_cache_hit_tokens, prompt_cache_miss_tokens
        metrics.cache_read_tokens = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
        metrics.uncached_input_tokens = miss
        metrics.total_input_tokens = metrics.cache_read_tokens + miss
        metrics.cache_hit = metrics.cache_read_tokens > 0

    elif canonical == "gemini":
        # Gemini reports: cached_content_token_count in usageMetadata
        metrics.cache_read_tokens = int(usage.get("cachedContentTokenCount", 0) or 0)
        metrics.total_input_tokens = int(usage.get("promptTokenCount", 0) or 0)
        metrics.uncached_input_tokens = metrics.total_input_tokens - metrics.cache_read_tokens
        metrics.cache_hit = metrics.cache_read_tokens > 0

    else:
        # Unknown provider — extract what we can
        metrics.total_input_tokens = int(
            usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
        )

    return metrics
