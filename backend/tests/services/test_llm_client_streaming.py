from __future__ import annotations

import pytest

from app.services.llm_client import LLMMessage, OpenAICompatibleClient


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
