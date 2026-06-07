import pytest

from app.services.llm_client import LLMMessage, LLMResponse, get_max_tokens


def test_get_max_tokens_clamps_oversized_override_for_custom_provider():
    assert get_max_tokens("custom", model="qwen3.6-plus", max_output_tokens=999999) == 65536


def test_get_max_tokens_preserves_valid_override():
    assert get_max_tokens("custom", model="qwen3.6-plus", max_output_tokens=32000) == 32000


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
    inner = _FakeClient([
        LLMResponse(content="partial", finish_reason="length"),
        LLMResponse(content="complete", finish_reason="stop"),
    ])
    client = _CapAwareLLMClient(inner, provider="custom", model="qwen3.6-plus")

    response = await client.complete(
        [LLMMessage(role="user", content="write a long report")],
        max_tokens=8192,
    )

    assert response.content == "complete"
    assert [call["max_tokens"] for call in inner.complete_calls] == [8192, 65536]
    snapshot = metrics.snapshot()
    assert snapshot["llm_output_cap_hit_total"]["custom:qwen3.6-plus:length:complete:initial"] == 1


@pytest.mark.asyncio
async def test_non_streaming_cap_retry_still_capped_marks_response():
    from app.memory import metrics
    from app.services.llm_client import _CapAwareLLMClient

    metrics.reset_all()
    inner = _FakeClient([
        LLMResponse(content="partial", finish_reason="max_tokens"),
        LLMResponse(content="still partial", finish_reason="length"),
    ])
    client = _CapAwareLLMClient(inner, provider="custom", model="qwen3.6-plus")

    response = await client.complete(
        [LLMMessage(role="user", content="write a long report")],
        max_tokens=8192,
    )

    assert response.finish_reason == "length"
    assert "Output truncated" in response.content
    assert [call["max_tokens"] for call in inner.complete_calls] == [8192, 65536]
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
