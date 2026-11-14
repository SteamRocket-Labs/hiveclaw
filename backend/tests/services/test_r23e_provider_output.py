"""One kernel-path protocol matrix, plus compatible-client regressions."""

import json

import httpx
import pytest

from app.services.llm_client import AnthropicClient, GeminiClient, LLMMessage, OpenAICompatibleClient, OpenAIResponsesClient

CLIENTS = {"openai": OpenAICompatibleClient, "responses": OpenAIResponsesClient,
           "gemini": GeminiClient, "anthropic": AnthropicClient}
MCP_CALL = {"type": "mcp_call"}
MESSAGE = {"type": "message", "content": [{"type": "output_text", "text": "Synthetic answer"}]}


def success_body(protocol, items=(), *, text=False, candidate_metadata=None):
    items = list(items)
    if protocol == "openai":
        return {"choices": [{"message": {"content": "Synthetic answer" if text else "", "tool_calls": items},
                             "finish_reason": "stop"}]}
    if text:
        items.append(MESSAGE if protocol == "responses" else {"type": "text", "text": "Synthetic answer"})
    if protocol == "responses":
        return {"status": "completed", "output": items}
    if protocol == "anthropic":
        return {"type": "message", "content": items, "stop_reason": "end_turn"}
    return {"candidates": [{"content": {"parts": items}, "finishReason": "STOP", **(candidate_metadata or {})}]}


def transport_response(protocol, body):
    if protocol == "responses":  # Its kernel stream() delegates to complete().
        return httpx.Response(200, json=body)
    if protocol == "openai":
        events = [{"choices": [{"delta": c["message"], "finish_reason": c["finish_reason"]} for c in body["choices"]]}]
    elif protocol == "gemini":
        events = [body, {"usageMetadata": {"totalTokenCount": 1}}]
    else:
        frames = []
        for index, block in enumerate(body["content"]):
            frames.append(f"event: content_block_start\ndata: {json.dumps({'index': index, 'content_block': block})}\n\n")
        return httpx.Response(200, text="".join(frames) + "event: message_stop\ndata: {}\n\n")
    return httpx.Response(200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n")


SECONDARY_EFFECT = success_body("openai")
SECONDARY_EFFECT["choices"].extend(success_body("openai", [MCP_CALL])["choices"])
FUNCTION = {"id": "local", "type": "function", "function": {"name": "local", "arguments": "{}"}}
CASES = [
    ("openai", success_body("openai", [MCP_CALL]), True),
    ("openai", SECONDARY_EFFECT, True),
    ("openai", success_body("openai", [FUNCTION]), False),
    ("openai", success_body("openai", [{**FUNCTION, "type": None}]), False),
    ("responses", success_body("responses", [MCP_CALL]), True),
    ("responses", success_body("responses", [MCP_CALL], text=True), True),
    ("responses", success_body("responses", [{"type": "mcp_list_tools"}]), False),
    ("gemini", success_body("gemini", [{"codeExecutionResult": {"outcome": "OUTCOME_OK"}}]), True),
    ("gemini", success_body("gemini", candidate_metadata={"groundingMetadata": {}, "urlContextMetadata": {}}), True),
    ("gemini", success_body("gemini", [{"thoughtSignature": "synthetic"}]), False),
    ("anthropic", success_body("anthropic", [{"type": "server_tool_use"}]), True),
    ("anthropic", success_body("anthropic", [{"type": "redacted_thinking"}]), False),
]
CASES = [(p, "complete" if p == "responses" else "stream", body, expected) for p, body, expected in CASES]
# Retain one native complete representative per distinct flag-assignment line, not a mode/kind cross-product.
CASES.extend([
    ("gemini", "complete", success_body("gemini", [{"codeExecutionResult": {"outcome": "OUTCOME_OK"}}]), True),
    ("anthropic", "complete", success_body("anthropic", [{"type": "server_tool_use"}]), True),
])


@pytest.mark.parametrize("protocol,method_name,body,expected", CASES,
                         ids=["compatible_primary", "compatible_secondary", "client_function", "client_function_null", "responses_effect",
                              "effect_with_text", "discovery", "gemini_code", "gemini_metadata", "thought_signature",
                              "anthropic_server", "redacted_thinking", "gemini_complete", "anthropic_complete"])
async def test_success_retains_provider_output_fact(protocol, method_name, body, expected):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=body) if method_name == "complete" else transport_response(protocol, body)

    client = CLIENTS[protocol](api_key="synthetic", model="synthetic", base_url="http://127.0.0.1/native")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        response = await getattr(client, method_name)([LLMMessage(role="user", content="Synthetic task")])
        assert response.provider_output_seen is expected and len(requests) == 1
        if expected:
            assert response.tool_calls == [], "Provider output must not be promoted into a client function"
        if body.get("output") == [MCP_CALL, MESSAGE]:
            assert response.content == "Synthetic answer"
        if protocol == "openai" and not expected:
            assert response.tool_calls == [FUNCTION]
    finally:
        await client.close()


@pytest.mark.parametrize("primary,secondary,expected_calls,seen", [
    ({"content": "Answer", "tool_calls": [None]}, [], [], False),
    ({"content": "Answer", "tool_calls": [0]}, [], [], False),
    ({"content": "Answer"}, [None], [], False),
    ({"content": "Answer"}, [{"delta": None}], [], False),
    ({"content": "Answer"}, [{"delta": {"tool_calls": 5}}], [], False),
    ({"content": "Answer", "tool_calls": [{"index": 0, **FUNCTION}, {"index": [1], "type": "mcp_call"}]},
     [], [FUNCTION], True),
], ids=["null_call", "zero_call", "null_choice", "null_delta", "non_list_calls", "unhashable_secondary_index"])
async def test_malformed_stream_classification_preserves_answer(primary, secondary, expected_calls, seen):
    body = {"choices": [{"delta": primary}, *secondary]}
    client = OpenAICompatibleClient(api_key="synthetic", base_url="http://127.0.0.1")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(
        200, text=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n")))
    try:
        response = await client.stream([LLMMessage(role="user", content="Synthetic task")])
        assert response.content == "Answer" and response.provider_output_seen is seen
        assert response.tool_calls == expected_calls
    finally:
        await client.close()


@pytest.mark.parametrize("null_type", [False, True], ids=["missing", "null"])
async def test_complete_ignores_untyped_calls_without_changing_error_rule(null_type):
    from app.services.llm_client import _api_error_delivery_state

    body = success_body("openai")
    call = {"function": {"name": "local", "arguments": "{}"}}
    if null_type:
        call["type"] = None
    body["choices"].extend(success_body("openai", [call])["choices"])
    client = OpenAICompatibleClient(api_key="synthetic", base_url="http://127.0.0.1")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body)))
    try:
        response = await client.complete([LLMMessage(role="user", content="Synthetic task")])
        assert response.provider_output_seen is False and not response.tool_calls and not response.content
        assert _api_error_delivery_state(body, "openai") == "unknown", "Error-path malformed kinds stay conservative"
        body["choices"][1]["message"]["tool_calls"][0]["type"] = "mcp_call"
        typed = await client.complete([LLMMessage(role="user", content="Synthetic task")])
        assert typed.provider_output_seen is True and not typed.tool_calls and not typed.content
    finally:
        await client.close()


async def test_provider_index_deltas_are_ignored_without_changing_function_collection():
    calls = [{"index": 0, "id": "remote", "type": "mcp_call"},
             {"index": 0, "function": {"name": "must_not_execute", "arguments": "{}"}},
             {"index": 1, **FUNCTION}, {"index": 1, "function": {"arguments": " "}},
             {"index": 0, "function": {"arguments": "more provider output"}},
             {"index": 1, "type": "mcp_call", "function": {"arguments": "tail"}}]
    wire = "".join(f"data: {json.dumps({'choices': [{'delta': {'tool_calls': [call]}}]})}\n\n" for call in calls)
    client = OpenAICompatibleClient(api_key="synthetic", base_url="http://127.0.0.1")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, text=wire)))
    try:
        response = await client.stream([LLMMessage(role="user", content="Synthetic task")])
        assert response.provider_output_seen is True
        assert response.tool_calls == [{**FUNCTION, "function": {"name": "local", "arguments": "{} tail"}}]
        # R23e S1: without pre-collection discard, index 1 creates a phantom index-0 function.
        single = {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "r", "type": "mcp_call"}]}}]}
        wire = f"data: {json.dumps(single)}\n\n"
        single_response = await client.stream([LLMMessage(role="user", content="Synthetic single frame")])
        assert single_response.provider_output_seen is True and single_response.tool_calls == []
    finally:
        await client.close()
