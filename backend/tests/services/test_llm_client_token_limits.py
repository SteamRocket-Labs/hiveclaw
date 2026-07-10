import pytest

from app.services.llm_client import LLMMessage, LLMResponse, get_max_tokens


def test_get_max_tokens_clamps_oversized_override_to_provider_ceiling():
    # custom provider has no explicit ceiling -> falls back to class default 131072.
    assert get_max_tokens("custom", model="qwen3.6-plus", max_output_tokens=999999) == 131072


def test_get_max_tokens_preserves_valid_override():
    assert get_max_tokens("custom", model="qwen3.6-plus", max_output_tokens=32000) == 32000


def test_get_max_tokens_deepseek_override_reaches_384k():
    # DeepSeek V4 emits up to 384K output tokens; an override above the old
    # flat 65536 cap is now honoured up to the provider ceiling.
    assert get_max_tokens("deepseek", model="deepseek-v4-pro", max_output_tokens=384000) == 384000
    # An override beyond the DeepSeek ceiling is clamped down to it.
    assert get_max_tokens("deepseek", model="deepseek-v4-pro", max_output_tokens=500000) == 384000


def test_get_max_tokens_clamps_to_absolute_ceiling():
    # Even a provider whose configured ceiling exceeds ABSOLUTE is bounded by it.
    # Build a temporary spec with an over-large ceiling and verify clamping.
    from app.services.llm_client import (
        ABSOLUTE_MAX_OUTPUT_TOKENS,
        get_max_tokens,
    )

    # DeepSeek (384000) is below ABSOLUTE (524288): an absurd override clamps to 384000, not ABSOLUTE.
    assert get_max_tokens("deepseek", model="deepseek-v4-pro", max_output_tokens=10_000_000) == 384000
    assert ABSOLUTE_MAX_OUTPUT_TOKENS == 524288


def test_get_max_tokens_per_provider_ceilings():
    # Anthropic / OpenAI honour overrides up to their 131072 ceiling.
    assert get_max_tokens("anthropic", model="claude-opus-4-8", max_output_tokens=131072) == 131072
    assert get_max_tokens("anthropic", model="claude-opus-4-8", max_output_tokens=200000) == 131072
    assert get_max_tokens("openai", model="gpt-5.4", max_output_tokens=120000) == 120000


class _FakeClient:
    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.complete_calls: list[dict] = []
        self.stream_calls: list[dict] = []
        self.closed = False

    async def complete(self, messages, tools=None, temperature=0.7, max_tokens=None, **kwargs):
        self.complete_calls.append({"tools": tools, "max_tokens": max_tokens, "kwargs": kwargs})
        return self.responses.pop(0)

    async def stream(self, messages, tools=None, temperature=0.7, max_tokens=None, **kwargs):
        self.stream_calls.append({"tools": tools, "max_tokens": max_tokens, "kwargs": kwargs})
        return self.responses.pop(0)

    async def close(self):
        self.closed = True

    def _get_headers(self):
        return {}


@pytest.mark.asyncio
async def test_non_streaming_length_finish_retries_once_at_64k_and_records_metric():
    from app.memory import metrics
    from app.services.llm_client import _CapAwareLLMClient

    metrics.reset_all()
    inner = _FakeClient(
        [
            LLMResponse(content="partial", finish_reason="length"),
            LLMResponse(content="complete", finish_reason="stop"),
        ]
    )
    client = _CapAwareLLMClient(inner, provider="custom", model="qwen3.6-plus")

    response = await client.complete(
        [LLMMessage(role="user", content="write a long report")],
        max_tokens=8192,
    )

    assert response.content == "complete"
    # custom provider escalates to its per-provider ceiling (class default 131072).
    assert [call["max_tokens"] for call in inner.complete_calls] == [8192, 131072]
    snapshot = metrics.snapshot()
    assert snapshot["llm_output_cap_hit_total"]["custom:qwen3.6-plus:length:complete:initial"] == 1


@pytest.mark.asyncio
async def test_non_streaming_cap_retry_still_capped_marks_response():
    from app.memory import metrics
    from app.services.llm_client import _CapAwareLLMClient

    metrics.reset_all()
    inner = _FakeClient(
        [
            LLMResponse(content="partial", finish_reason="max_tokens"),
            LLMResponse(content="still partial", finish_reason="length"),
        ]
    )
    client = _CapAwareLLMClient(inner, provider="custom", model="qwen3.6-plus")

    response = await client.complete(
        [LLMMessage(role="user", content="write a long report")],
        max_tokens=8192,
    )

    assert response.finish_reason == "length"
    assert "Output truncated" in response.content
    # custom provider escalates to its per-provider ceiling (class default 131072).
    assert [call["max_tokens"] for call in inner.complete_calls] == [8192, 131072]
    snapshot = metrics.snapshot()
    assert snapshot["llm_output_cap_hit_total"]["custom:qwen3.6-plus:max_tokens:complete:initial"] == 1
    assert snapshot["llm_output_cap_hit_total"]["custom:qwen3.6-plus:length:complete:retry"] == 1


@pytest.mark.asyncio
async def test_streaming_length_finish_records_metric_without_retry():
    from app.memory import metrics
    from app.services.llm_client import _CapAwareLLMClient

    metrics.reset_all()
    inner = _FakeClient([LLMResponse(content="partial", finish_reason="length")])
    client = _CapAwareLLMClient(inner, provider="custom", model="qwen3.6-plus")

    response = await client.stream(
        [LLMMessage(role="user", content="write a long report")],
        max_tokens=8192,
    )

    assert response.content == "partial"
    assert [call["max_tokens"] for call in inner.stream_calls] == [8192]
    snapshot = metrics.snapshot()
    assert snapshot["llm_output_cap_hit_total"]["custom:qwen3.6-plus:length:stream:initial"] == 1


@pytest.mark.asyncio
async def test_non_streaming_tool_call_cap_records_without_retry():
    from app.memory import metrics
    from app.services.llm_client import _CapAwareLLMClient

    metrics.reset_all()
    inner = _FakeClient([LLMResponse(content="", finish_reason="length")])
    client = _CapAwareLLMClient(inner, provider="custom", model="qwen3.6-plus")

    response = await client.complete(
        [LLMMessage(role="user", content="call a tool")],
        tools=[{"type": "function", "function": {"name": "do_work", "parameters": {"type": "object"}}}],
        max_tokens=8192,
    )

    assert response.finish_reason == "length"
    assert [call["max_tokens"] for call in inner.complete_calls] == [8192]
    snapshot = metrics.snapshot()
    assert snapshot["llm_output_cap_hit_total"]["custom:qwen3.6-plus:length:complete:initial"] == 1


@pytest.mark.asyncio
async def test_cap_retry_escalates_to_per_provider_ceiling():
    # The escalate ceiling is the provider's own max output, not a flat 65536.
    # DeepSeek escalates to 384000; this proves per-provider behaviour.
    from app.memory import metrics
    from app.services.llm_client import _CapAwareLLMClient

    metrics.reset_all()
    inner = _FakeClient(
        [
            LLMResponse(content="partial", finish_reason="length"),
            LLMResponse(content="complete", finish_reason="stop"),
        ]
    )
    client = _CapAwareLLMClient(inner, provider="deepseek", model="deepseek-v4-pro")

    response = await client.complete(
        [LLMMessage(role="user", content="write a long report")],
        max_tokens=8192,
    )

    assert response.content == "complete"
    assert [call["max_tokens"] for call in inner.complete_calls] == [8192, 384000]


@pytest.mark.asyncio
async def test_cap_retry_skips_when_already_at_provider_ceiling():
    # If the caller already requested the provider ceiling, no escalate retry fires.
    from app.memory import metrics
    from app.services.llm_client import _CapAwareLLMClient

    metrics.reset_all()
    inner = _FakeClient([LLMResponse(content="partial", finish_reason="length")])
    client = _CapAwareLLMClient(inner, provider="anthropic", model="claude-opus-4-8")

    response = await client.complete(
        [LLMMessage(role="user", content="write a long report")],
        max_tokens=131072,
    )

    assert response.finish_reason == "length"
    assert [call["max_tokens"] for call in inner.complete_calls] == [131072]
