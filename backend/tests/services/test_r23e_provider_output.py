"""Successful provider execution stays visible after protocol projection."""

import json

import httpx
import pytest

from app.services.llm_client import (
    AnthropicClient,
    GeminiClient,
    LLMMessage,
    OpenAICompatibleClient,
    OpenAIResponsesClient,
)

CLIENTS = {
    "openai": OpenAICompatibleClient,
    "responses": OpenAIResponsesClient,
    "gemini": GeminiClient,
    "anthropic": AnthropicClient,
}
EFFECT_ITEMS = {
    "openai": [{"type": "mcp_call"}],
    "responses": [{"type": kind} for kind in (
        "mcp_call", "web_search_call", "file_search_call", "code_interpreter_call", "image_generation_call", "future_item",
    )],
    "gemini": [
        {"executableCode": {"language": "PYTHON", "code": "print(1)"}},
        {"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": "1"}},
    ],
    "anthropic": [{"type": kind} for kind in (
        "server_tool_use", "mcp_tool_use", "web_search_tool_result", "future_tool_result",
    )],
}
TEXT_ITEMS = {
    "openai": {"content": "Synthetic answer"},
    "responses": {"type": "message", "content": [{"type": "output_text", "text": "Synthetic answer"}]},
    "gemini": {"text": "Synthetic answer"},
    "anthropic": {"type": "text", "text": "Synthetic answer"},
}
FUNCTION_ITEMS = {
    "openai": {"id": "call_local", "type": "function", "function": {"name": "local", "arguments": "{}"}},
    "responses": {"type": "function_call", "call_id": "call_local", "name": "local", "arguments": "{}"},
    "gemini": {"functionCall": {"name": "local", "args": {}}},
    "anthropic": {"type": "tool_use", "id": "call_local", "name": "local", "input": {}},
}


def success_body(protocol, items=(), *, text=False, candidate_metadata=None):
    items = list(items)
    if protocol == "openai":
        message = {"content": "Synthetic answer" if text else "", "tool_calls": items}
        return {"choices": [{"message": message, "finish_reason": "stop"}]}
    if text:
        items.append(TEXT_ITEMS[protocol])
    if protocol == "responses":
        return {"status": "completed", "output": items}
    if protocol == "anthropic":
        return {"type": "message", "content": items, "stop_reason": "end_turn"}
    return {"candidates": [{"content": {"parts": items}, "finishReason": "STOP", **(candidate_metadata or {})}]}


def transport_response(protocol, body, *, stream):
    # Responses.stream currently uses complete(), not a native SSE parser.
    if not stream or protocol == "responses":
        return httpx.Response(200, json=body)
    events = []
    if protocol == "openai":
        events.append({"choices": [
            {"delta": choice["message"], "finish_reason": choice["finish_reason"]} for choice in body["choices"]
        ]})
        events.append({"choices": [], "usage": {"total_tokens": 1}})
    elif protocol == "gemini":
        events = [body, {"usageMetadata": {"totalTokenCount": 1}}]
    else:
        def event(kind, **payload):
            return f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n"

        frames = [event("message_start", message={"model": "synthetic"})]
        for index, block in enumerate(body["content"]):
            frames.append(event("content_block_start", index=index, content_block=block))
            if block["type"] == "text":
                delta = {"type": "text_delta", "text": block["text"]}
            elif block["type"] == "tool_use":
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            else:
                continue
            frames.append(event("content_block_delta", index=index, delta=delta))
        frames.extend([event("message_delta", delta={"stop_reason": "end_turn"}), event("message_stop")])
        return httpx.Response(200, text="".join(frames), headers={"content-type": "text/event-stream"})
    text = "".join(f"data: {json.dumps(data)}\n\n" for data in events) + "data: [DONE]\n\n"
    return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})


SECONDARY_COMPATIBLE_EFFECT = success_body("openai")
SECONDARY_COMPATIBLE_EFFECT["choices"].extend(success_body("openai", EFFECT_ITEMS["openai"])["choices"])
CASES = [pytest.param("openai", SECONDARY_COMPATIBLE_EFFECT, True, id="compatible_secondary_effect")]
for protocol, items in EFFECT_ITEMS.items():
    for item in items:
        name = item.get("type") or next(iter(item))
        CASES.append(pytest.param(protocol, success_body(protocol, [item]), True, id=f"{protocol}_{name}"))
    for companion in ("text", "function"):
        body = success_body(protocol, [items[0]], text=companion == "text")
        if companion == "function":
            body = success_body(protocol, [items[0], FUNCTION_ITEMS[protocol]])
        CASES.append(pytest.param(protocol, body, True, id=f"{protocol}_effect_with_{companion}"))
    CASES.append(pytest.param(protocol, success_body(protocol), False, id=f"{protocol}_empty"))
    CASES.append(pytest.param(
        protocol, success_body(protocol, [FUNCTION_ITEMS[protocol]]), False, id=f"{protocol}_client_call",
    ))
for kind in ("groundingMetadata", "urlContextMetadata"):
    CASES.append(pytest.param(
        "gemini", success_body("gemini", candidate_metadata={kind: {}}), True, id=f"gemini_{kind}",
    ))
CASES.extend([
    pytest.param("responses", success_body("responses", [{"type": "mcp_list_tools"}]), False, id="mcp_discovery"),
    pytest.param("responses", success_body("responses", [{"type": "reasoning"}]), False, id="responses_reasoning"),
    pytest.param("gemini", success_body("gemini", [{"thoughtSignature": "synthetic"}]), False, id="thought_signature"),
    pytest.param("anthropic", success_body("anthropic", [{"type": "redacted_thinking"}]), False, id="redacted_thinking"),
])


@pytest.mark.parametrize("protocol,body,expected", CASES)
@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
async def test_success_retains_provider_output_fact(protocol, body, expected, stream):
    requests = []

    def respond(request):
        requests.append(request)
        return transport_response(protocol, body, stream=stream)

    client = CLIENTS[protocol](api_key="synthetic-key", model="synthetic", base_url="http://127.0.0.1/native")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        method = client.stream if stream else client.complete
        response = await method([LLMMessage(role="user", content="Synthetic task")])
        assert response.provider_output_seen is expected
        assert len(requests) == 1
        if "Synthetic answer" in json.dumps(body):
            assert response.content == "Synthetic answer"
        if "call_local" in json.dumps(body) or "functionCall" in json.dumps(body):
            assert response.tool_calls
    finally:
        await client.close()


@pytest.mark.parametrize("effect_in_continuation", [False, True], ids=["initial", "continuation"])
async def test_output_cap_merge_keeps_provider_output_fact(effect_in_continuation):
    from app.kernel.engine import _merge_continuation_response

    client = OpenAIResponsesClient(api_key="synthetic-key")
    effect = success_body("responses", EFFECT_ITEMS["responses"][:1])
    empty = success_body("responses")
    initial = client._parse_response_data(empty if effect_in_continuation else effect)
    continuation = client._parse_response_data(effect if effect_in_continuation else empty)
    merged = _merge_continuation_response(initial, continuation)
    assert merged.provider_output_seen is True
    assert not merged.content and not merged.tool_calls
