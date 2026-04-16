"""Tests for provider-agnostic prompt caching layer.

Design principle: the cache layer is capability-driven, not name-driven.
Any unknown provider should work safely (passthrough for hints, universal
probe for metrics).
"""

from __future__ import annotations

from app.services.llm_client import LLMMessage
from app.services.prompt_cache import (
    PROMPT_CACHE_BOUNDARY,
    CacheMetrics,
    _supports_cache_control,
    apply_cache_hints,
    extract_cache_metrics,
)


class TestCapabilityDetection:
    """Only providers that accept cache_control blocks should be detected."""

    def test_anthropic_supported(self):
        assert _supports_cache_control("anthropic") is True
        assert _supports_cache_control("claude-3.5-sonnet") is True

    def test_qwen_supported(self):
        """Qwen/DashScope supports Anthropic-style cache_control markers."""
        assert _supports_cache_control("qwen") is True
        assert _supports_cache_control("dashscope") is True
        assert _supports_cache_control("qwen-max") is True

    def test_minimax_supported(self):
        """MiniMax supports cache_control via Anthropic-compatible SDK."""
        assert _supports_cache_control("minimax") is True

    def test_auto_cache_providers_no_markers(self):
        """Providers with automatic caching do NOT need explicit markers."""
        for p in ("openai", "deepseek", "gemini", "zhipu", "kimi", "moonshot"):
            assert _supports_cache_control(p) is False, f"{p} should not need cache_control markers"

    def test_unknown_provider_safe_default(self):
        assert _supports_cache_control("totally-new-provider-2027") is False
        assert _supports_cache_control("") is False


class TestHintInjection:
    def _system_msg(self) -> LLMMessage:
        return LLMMessage(
            role="system",
            content=f"frozen part\n{PROMPT_CACHE_BOUNDARY}\ndynamic part",
        )

    def test_anthropic_conversation_5min(self):
        result = apply_cache_hints([self._system_msg()], "anthropic", execution_mode="conversation")
        cc = result[0].content[0]["cache_control"]
        assert cc == {"type": "ephemeral"}

    def test_anthropic_heartbeat_1h(self):
        result = apply_cache_hints([self._system_msg()], "anthropic", execution_mode="heartbeat")
        cc = result[0].content[0]["cache_control"]
        assert cc == {"type": "ephemeral", "ttl": "1h"}

    def test_anthropic_trigger_1h(self):
        result = apply_cache_hints([self._system_msg()], "anthropic", execution_mode="trigger")
        assert result[0].content[0]["cache_control"]["ttl"] == "1h"

    def test_splits_at_boundary(self):
        result = apply_cache_hints([self._system_msg()], "anthropic")
        blocks = result[0].content
        assert len(blocks) == 2
        assert "frozen part" in blocks[0]["text"]
        assert "dynamic part" in blocks[1]["text"]
        assert "cache_control" in blocks[0]
        assert "cache_control" not in blocks[1]

    def test_last_3_messages_get_cache_control(self):
        msgs = [
            self._system_msg(),
            LLMMessage(role="user", content="msg1"),
            LLMMessage(role="assistant", content="reply1"),
            LLMMessage(role="user", content="msg2"),
            LLMMessage(role="assistant", content="reply2"),
            LLMMessage(role="user", content="msg3"),
        ]
        result = apply_cache_hints(msgs, "anthropic")
        for i in [-1, -2, -3]:
            assert isinstance(result[i].content, list)
            assert "cache_control" in result[i].content[0]
        # Earlier messages untouched
        assert isinstance(result[1].content, str)

    def test_unknown_provider_passthrough(self):
        msg = LLMMessage(role="system", content="hello")
        result = apply_cache_hints([msg], "totally-new-llm-2027")
        assert result[0].content == "hello"

    def test_openai_passthrough(self):
        msg = LLMMessage(role="system", content="hello")
        result = apply_cache_hints([msg], "openai")
        assert result[0].content == "hello"

    def test_explicit_override_enables_cache_control(self):
        """A new provider can opt in via override without touching _CACHE_CONTROL_PROVIDERS."""
        msg = self._system_msg()
        result = apply_cache_hints([msg], "future-provider", supports_cache_control_override=True)
        assert isinstance(result[0].content, list)
        assert "cache_control" in result[0].content[0]

    def test_explicit_override_disables_cache_control(self):
        """Anthropic can be opted out (e.g., for testing) via override."""
        msg = self._system_msg()
        result = apply_cache_hints([msg], "anthropic", supports_cache_control_override=False)
        assert result[0].content == msg.content  # unchanged


class TestUniversalMetricsProbe:
    """Metrics extraction should work for ANY provider via field probing."""

    def test_anthropic_format(self):
        m = extract_cache_metrics(
            {"input_tokens": 1000, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 50},
            "anthropic",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 800
        assert m.cache_write_tokens == 50

    def test_openai_format(self):
        m = extract_cache_metrics(
            {"prompt_tokens": 500, "prompt_tokens_details": {"cached_tokens": 400}},
            "openai",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 400
        assert m.hit_rate == 0.8

    def test_deepseek_format(self):
        m = extract_cache_metrics(
            {"prompt_cache_hit_tokens": 600, "prompt_cache_miss_tokens": 100},
            "deepseek",
        )
        assert m.cache_hit is True
        assert m.total_input_tokens == 700

    def test_gemini_format(self):
        m = extract_cache_metrics(
            {"promptTokenCount": 1000, "cachedContentTokenCount": 500},
            "gemini",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 500

    def test_zhipu_glm_format(self):
        """Zhipu GLM uses OpenAI-compatible prompt_tokens_details.cached_tokens."""
        m = extract_cache_metrics(
            {"prompt_tokens": 800, "prompt_tokens_details": {"cached_tokens": 500}},
            "zhipu/glm-5",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 500

    def test_kimi_top_level_cached_tokens(self):
        """Kimi reports cached_tokens at top level (some models)."""
        m = extract_cache_metrics(
            {"prompt_tokens": 600, "cached_tokens": 450},
            "kimi-k2.5",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 450

    def test_qwen_nested_write_tokens(self):
        """Qwen reports cache_creation_input_tokens nested in prompt_tokens_details."""
        m = extract_cache_metrics(
            {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 700, "cache_creation_input_tokens": 200}},
            "qwen-max",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 700
        assert m.cache_write_tokens == 200

    def test_minimax_anthropic_compat_format(self):
        """MiniMax in Anthropic-compat mode uses cache_read_input_tokens."""
        m = extract_cache_metrics(
            {"input_tokens": 900, "cache_read_input_tokens": 700, "cache_creation_input_tokens": 100},
            "minimax",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 700
        assert m.cache_write_tokens == 100

    def test_minimax_openai_compat_format(self):
        """MiniMax in OpenAI-compat mode uses prompt_tokens_details.cached_tokens."""
        m = extract_cache_metrics(
            {"prompt_tokens": 800, "prompt_tokens_details": {"cached_tokens": 600}},
            "minimax",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 600

    def test_unknown_provider_with_standard_fields(self):
        """A brand-new provider using OpenAI-compatible fields should work."""
        m = extract_cache_metrics(
            {"prompt_tokens": 800, "prompt_tokens_details": {"cached_tokens": 600}},
            "brand-new-llm-2027",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 600
        assert m.provider == "brand-new-llm-2027"

    def test_unknown_provider_with_anthropic_fields(self):
        """A new provider adopting Anthropic field names also works."""
        m = extract_cache_metrics(
            {"input_tokens": 500, "cache_read_input_tokens": 400},
            "some-anthropic-compatible-llm",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 400

    def test_unknown_provider_no_cache_fields(self):
        """Provider with only basic token counts — cache_hit=False, total still extracted."""
        m = extract_cache_metrics(
            {"prompt_tokens": 300},
            "minimal-llm",
        )
        assert m.cache_hit is False
        assert m.total_input_tokens == 300
        assert m.cache_read_tokens == 0

    def test_empty_usage(self):
        m = extract_cache_metrics(None, "any-provider")
        assert m.cache_hit is False
        assert m.total_input_tokens == 0

    def test_as_log_dict_structure(self):
        m = CacheMetrics(
            provider="test",
            cache_write_tokens=100,
            cache_read_tokens=800,
            uncached_input_tokens=100,
            total_input_tokens=1000,
            cache_hit=True,
        )
        d = m.as_log_dict()
        assert d["cache_hit"] is True
        assert d["cache_hit_rate"] == 0.8
        assert set(d.keys()) == {
            "provider", "cache_write_tokens", "cache_read_tokens",
            "uncached_input_tokens", "total_input_tokens",
            "cache_hit", "cache_hit_rate",
        }
