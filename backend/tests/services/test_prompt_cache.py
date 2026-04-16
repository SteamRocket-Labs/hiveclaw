"""Tests for provider-agnostic prompt caching layer."""

from __future__ import annotations

from app.services.llm_client import LLMMessage
from app.services.prompt_cache import (
    PROMPT_CACHE_BOUNDARY,
    CacheMetrics,
    _detect_provider,
    apply_cache_hints,
    extract_cache_metrics,
)


class TestProviderDetection:
    def test_anthropic_variants(self):
        for p in ("anthropic", "Anthropic", "claude-3.5-sonnet", "CLAUDE"):
            assert _detect_provider(p) == "anthropic", f"failed for {p}"

    def test_openai_variants(self):
        for p in ("openai", "OpenAI", "gpt-4.1", "GPT"):
            assert _detect_provider(p) == "openai", f"failed for {p}"

    def test_deepseek(self):
        assert _detect_provider("deepseek-coder") == "deepseek"

    def test_gemini(self):
        for p in ("gemini-pro", "google-gemini", "Google"):
            assert _detect_provider(p) == "gemini", f"failed for {p}"

    def test_zhipu(self):
        for p in ("zhipu", "glm-5.1"):
            assert _detect_provider(p) == "zhipu", f"failed for {p}"

    def test_minimax(self):
        assert _detect_provider("minimax-abab") == "minimax"

    def test_unknown(self):
        assert _detect_provider("some-random-llm") == "unknown"
        assert _detect_provider("") == "unknown"


class TestAnthropicHints:
    def _system_msg(self) -> LLMMessage:
        return LLMMessage(
            role="system",
            content=f"frozen part\n{PROMPT_CACHE_BOUNDARY}\ndynamic part",
        )

    def test_conversation_mode_uses_5min_ttl(self):
        result = apply_cache_hints([self._system_msg()], "anthropic", execution_mode="conversation")
        cc = result[0].content[0]["cache_control"]
        assert cc == {"type": "ephemeral"}
        assert "ttl" not in cc

    def test_heartbeat_mode_uses_1h_ttl(self):
        result = apply_cache_hints([self._system_msg()], "anthropic", execution_mode="heartbeat")
        cc = result[0].content[0]["cache_control"]
        assert cc == {"type": "ephemeral", "ttl": "1h"}

    def test_trigger_mode_uses_1h_ttl(self):
        result = apply_cache_hints([self._system_msg()], "anthropic", execution_mode="trigger")
        cc = result[0].content[0]["cache_control"]
        assert cc["ttl"] == "1h"

    def test_task_mode_uses_1h_ttl(self):
        result = apply_cache_hints([self._system_msg()], "anthropic", execution_mode="task")
        cc = result[0].content[0]["cache_control"]
        assert cc["ttl"] == "1h"

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
        # Last 3 non-system messages should have cache_control
        for i in [-1, -2, -3]:
            assert isinstance(result[i].content, list), f"msg at {i} not wrapped"
            assert "cache_control" in result[i].content[0]
        # Earlier messages should be untouched
        assert isinstance(result[1].content, str)


class TestNonAnthropicPassthrough:
    def test_openai_unchanged(self):
        msg = LLMMessage(role="system", content="hello")
        result = apply_cache_hints([msg], "openai")
        assert result[0].content == "hello"

    def test_deepseek_unchanged(self):
        msg = LLMMessage(role="system", content="hello")
        result = apply_cache_hints([msg], "deepseek")
        assert result[0].content == "hello"

    def test_unknown_unchanged(self):
        msg = LLMMessage(role="system", content="hello")
        result = apply_cache_hints([msg], "some-random-llm")
        assert result[0].content == "hello"


class TestCacheMetrics:
    def test_anthropic_cache_hit(self):
        m = extract_cache_metrics(
            {"input_tokens": 1000, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 50},
            "anthropic",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 800
        assert m.cache_write_tokens == 50
        assert m.hit_rate > 0.7

    def test_anthropic_cache_miss(self):
        m = extract_cache_metrics(
            {"input_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 900},
            "anthropic",
        )
        assert m.cache_hit is False
        assert m.cache_write_tokens == 900

    def test_openai_cached_tokens(self):
        m = extract_cache_metrics(
            {"prompt_tokens": 500, "prompt_tokens_details": {"cached_tokens": 400}},
            "openai",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 400
        assert m.hit_rate == 0.8

    def test_deepseek_cache(self):
        m = extract_cache_metrics(
            {"prompt_cache_hit_tokens": 600, "prompt_cache_miss_tokens": 100},
            "deepseek",
        )
        assert m.cache_hit is True
        assert m.total_input_tokens == 700
        assert abs(m.hit_rate - 600 / 700) < 0.01

    def test_gemini_cached_content(self):
        m = extract_cache_metrics(
            {"promptTokenCount": 1000, "cachedContentTokenCount": 500},
            "gemini",
        )
        assert m.cache_hit is True
        assert m.cache_read_tokens == 500

    def test_empty_usage(self):
        m = extract_cache_metrics(None, "anthropic")
        assert m.cache_hit is False
        assert m.total_input_tokens == 0

    def test_unknown_provider_graceful(self):
        m = extract_cache_metrics({"prompt_tokens": 100}, "minimax")
        assert m.provider == "minimax"
        assert m.total_input_tokens == 100
        assert m.cache_hit is False

    def test_as_log_dict_structure(self):
        m = CacheMetrics(
            provider="anthropic",
            cache_write_tokens=100,
            cache_read_tokens=800,
            uncached_input_tokens=100,
            total_input_tokens=1000,
            cache_hit=True,
        )
        d = m.as_log_dict()
        assert d["provider"] == "anthropic"
        assert d["cache_hit"] is True
        assert d["cache_hit_rate"] == 0.8
        assert set(d.keys()) == {
            "provider", "cache_write_tokens", "cache_read_tokens",
            "uncached_input_tokens", "total_input_tokens",
            "cache_hit", "cache_hit_rate",
        }
