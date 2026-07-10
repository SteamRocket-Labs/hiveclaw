from __future__ import annotations

import httpx
import pytest

from app.services.llm_client import AnthropicClient, LLMMessage, OpenAICompatibleClient, STREAM_RETRY_TOMBSTONE


class _FakeStreamResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"web_search","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"web_search","arguments":"{\\"query\\""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"embodied world model\\"}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        for line in lines:
            yield line

    async def aiter_bytes(self):
        if False:
            yield b""


class _FakeClient:
    def stream(self, *_args, **_kwargs):
        return _FakeStreamResponse()


class _FakeStatusResponse:
    def __init__(
        self, status_code: int, *, body: str = "", lines: list[str] | None = None, headers: dict | None = None
    ):
        self.status_code = status_code
        self._body = body
        self._lines = lines or []
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aiter_bytes(self):
        yield self._body.encode()


class _RetryStatusClient:
    def __init__(self):
        self.calls = 0

    def stream(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return _FakeStatusResponse(429, body="rate limited", headers={"retry-after": "0"})
        return _FakeStatusResponse(
            200,
            lines=[
                'data: {"choices":[{"delta":{"content":"ok"}}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                "data: [DONE]",
            ],
        )


class _FakePostResponse:
    def __init__(self, status_code: int, *, text: str = "", data: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._data = data or {}
        self.headers = headers or {}

    def json(self):
        return self._data


class _RetryPostStatusClient:
    def __init__(self):
        self.calls = 0

    async def post(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return _FakePostResponse(503, text="overloaded", headers={"retry-after": "0"})
        return _FakePostResponse(
            200,
            data={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )


def test_anthropic_headers_enable_interleaved_thinking_beta():
    client = AnthropicClient(api_key="test-key", model="claude-sonnet-4")

    headers = client._get_headers()

    assert headers["anthropic-beta"] == "interleaved-thinking-2025-05-14"


class _InterruptedStreamResponse:
    status_code = 200
    headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"partial "}}]}'
        raise httpx.ReadError("stream interrupted")

    async def aiter_bytes(self):
        if False:
            yield b""


class _SuccessfulRetryStreamResponse:
    status_code = 200
    headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"partial "}}]}'
        yield 'data: {"choices":[{"delta":{"content":"answer"}}]}'
        yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
        yield "data: [DONE]"

    async def aiter_bytes(self):
        if False:
            yield b""


class _InterruptedThenSuccessfulStreamClient:
    def __init__(self):
        self.calls = 0

    def stream(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return _InterruptedStreamResponse()
        return _SuccessfulRetryStreamResponse()


@pytest.mark.asyncio
async def test_openai_compatible_streaming_deduplicates_full_tool_names(monkeypatch):
    client = OpenAICompatibleClient(api_key="test", model="gpt-test", base_url="https://example.test/v1")

    async def fake_get_client():
        return _FakeClient()

    monkeypatch.setattr(client, "_get_client", fake_get_client)

    response = await client.stream(
        [LLMMessage(role="user", content="search")],
        tools=[{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}],
        temperature=0.7,
        max_tokens=16,
    )

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "web_search",
                "arguments": '{"query":"embodied world model"}',
            },
        }
    ]


def test_anthropic_format_omits_unsigned_thinking_block():
    message = LLMMessage(role="assistant", content="done", reasoning_content="private chain")

    payload = message.to_anthropic_format()

    assert payload == {"role": "assistant", "content": "done"}


def test_anthropic_format_preserves_signed_thinking_block():
    message = LLMMessage(
        role="assistant",
        content="done",
        reasoning_content="signed private chain",
        reasoning_signature="sig-123",
    )

    payload = message.to_anthropic_format()

    assert payload == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "signed private chain", "signature": "sig-123"},
            {"type": "text", "text": "done"},
        ],
    }


def test_anthropic_format_preserves_cache_control_content_blocks():
    message = LLMMessage(
        role="assistant",
        content=[{"type": "text", "text": "done", "cache_control": {"type": "ephemeral"}}],
        reasoning_content="signed private chain",
        reasoning_signature="sig-123",
    )

    payload = message.to_anthropic_format()

    assert payload == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "signed private chain", "signature": "sig-123"},
            {"type": "text", "text": "done", "cache_control": {"type": "ephemeral"}},
        ],
    }


def test_anthropic_format_preserves_single_cache_control_block_without_thinking():
    message = LLMMessage(
        role="assistant",
        content=[{"type": "text", "text": "done", "cache_control": {"type": "ephemeral"}}],
    )

    payload = message.to_anthropic_format()

    assert payload == {
        "role": "assistant",
        "content": [{"type": "text", "text": "done", "cache_control": {"type": "ephemeral"}}],
    }


@pytest.mark.asyncio
async def test_openai_compatible_streaming_retries_http_status_errors(monkeypatch):
    retry_client = _RetryStatusClient()
    client = OpenAICompatibleClient(api_key="test", model="gpt-test", base_url="https://example.test/v1")

    async def fake_get_client():
        return retry_client

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_get_client", fake_get_client)
    monkeypatch.setattr("app.services.llm_client.asyncio.sleep", fake_sleep)

    response = await client.stream(
        [LLMMessage(role="user", content="hello")],
        temperature=0.7,
        max_tokens=16,
    )

    assert retry_client.calls == 2
    assert response.content == "ok"
    assert response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_openai_compatible_streaming_retry_tombstones_partial_content(monkeypatch):
    retry_client = _InterruptedThenSuccessfulStreamClient()
    client = OpenAICompatibleClient(api_key="test", model="gpt-test", base_url="https://example.test/v1")
    chunks: list[str] = []

    async def fake_get_client():
        return retry_client

    async def fake_sleep(_seconds):
        return None

    async def on_chunk(text: str):
        chunks.append(text)

    monkeypatch.setattr(client, "_get_client", fake_get_client)
    monkeypatch.setattr("app.services.llm_client.asyncio.sleep", fake_sleep)

    response = await client.stream(
        [LLMMessage(role="user", content="hello")],
        temperature=0.7,
        max_tokens=16,
        on_chunk=on_chunk,
    )

    assert retry_client.calls == 2
    assert response.content == "partial answer"
    assert chunks == ["partial ", STREAM_RETRY_TOMBSTONE, "partial ", "answer"]
    assert STREAM_RETRY_TOMBSTONE not in response.content


@pytest.mark.asyncio
async def test_openai_compatible_complete_retries_http_status_errors(monkeypatch):
    retry_client = _RetryPostStatusClient()
    client = OpenAICompatibleClient(api_key="test", model="gpt-test", base_url="https://example.test/v1")

    async def fake_get_client():
        return retry_client

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_get_client", fake_get_client)
    monkeypatch.setattr("app.services.llm_client.asyncio.sleep", fake_sleep)

    response = await client.complete(
        [LLMMessage(role="user", content="hello")],
        temperature=0.7,
        max_tokens=16,
    )

    assert retry_client.calls == 2
    assert response.content == "ok"
    assert response.finish_reason == "stop"
