"""Only provider execution or unrecognised output makes an API error unknown."""

import json

import httpx
import pytest

from app.services.llm_client import (
    AnthropicClient,
    GeminiClient,
    LLMError,
    LLMMessage,
    OpenAICompatibleClient,
    OpenAIResponsesClient,
)

CLIENTS = [OpenAICompatibleClient, OpenAIResponsesClient, GeminiClient, AnthropicClient]
COMPLETED_EFFECT = {
    "type": "mcp_call",
    "id": "synthetic-call",
    "status": "completed",
    "name": "write_record",
    "output": "Synthetic record written",
}


def _error_payload(client_type, kind):
    data = {"error": {"message": "Synthetic provider failure"}}
    if client_type is OpenAICompatibleClient:
        message = {
            "completed_effect": {"tool_calls": [COMPLETED_EFFECT]},
            "unknown_type": {"tool_calls": [{"type": "future_tool"}]},
            "malformed_entry": "Not a message object",
            "partial_text": {"content": "Partial output"},
            "function_call": {
                "tool_calls": [{"type": "function", "function": {"name": "local_function", "arguments": "{}"}}]
            },
            "reasoning": {"reasoning_content": "Synthetic reasoning"},
            "empty": {},
        }[kind]
        data["choices"] = [] if kind == "empty" else [{"message": message}]
    elif client_type is OpenAIResponsesClient:
        item = {
            "completed_effect": COMPLETED_EFFECT,
            "unknown_type": {"type": "future_item"},
            "malformed_entry": None,
            "partial_text": {"type": "message", "content": [{"type": "output_text", "text": "Partial output"}]},
            "function_call": {"type": "function_call", "name": "local_function", "arguments": "{}"},
            "reasoning": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Synthetic reasoning"}]},
            "empty": {},
        }[kind]
        data.update(status="failed", output=[] if kind == "empty" else [item])
    elif client_type is GeminiClient:
        part = {
            "completed_effect": {"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": "Synthetic result"}},
            "unknown_type": {"futurePart": "Synthetic output"},
            "malformed_entry": None,
            "partial_text": {"text": "Partial output"},
            "function_call": {"functionCall": {"name": "local_function", "args": {}}},
            "reasoning": {"text": "Synthetic reasoning", "thought": True},
            "empty": {},
        }[kind]
        data["candidates"] = [] if kind == "empty" else [{"content": {"parts": [part]}}]
    else:
        block = {
            "completed_effect": {"type": "server_tool_use", "name": "web_search", "status": "completed"},
            "unknown_type": {"type": "future_block"},
            "malformed_entry": None,
            "partial_text": {"type": "text", "text": "Partial output"},
            "function_call": {"type": "tool_use", "name": "local_function", "input": {}},
            "reasoning": {"type": "thinking", "thinking": "Synthetic reasoning"},
            "empty": {},
        }[kind]
        data.update(type="error", content=[] if kind == "empty" else [block])
    return data


async def _observe_error(client_type, payload, *, status_code=200):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status_code) if payload is None else httpx.Response(status_code, json=payload)

    client = client_type(api_key="synthetic-key", base_url="http://127.0.0.1/native", model="synthetic")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(LLMError) as caught:
            await client.complete([LLMMessage(role="user", content="Synthetic task")], _http_max_attempts=1)
        assert len(requests) == 1
        return caught.value
    finally:
        await client.close()


@pytest.mark.parametrize("client_type", CLIENTS, ids=lambda client: client.__name__)
@pytest.mark.parametrize(
    "kind,expected",
    [
        ("completed_effect", "unknown"),
        ("unknown_type", "unknown"),
        ("malformed_entry", "unknown"),
        ("partial_text", "rejected"),
        ("function_call", "rejected"),
        ("reasoning", "rejected"),
    ],
)
async def test_api_error_delivery_follows_protocol_output(client_type, kind, expected):
    # Controller decision 1: partial text, reasoning and client calls retain safe fallback.
    error = await _observe_error(client_type, _error_payload(client_type, kind))
    assert error.delivery_state == expected


@pytest.mark.parametrize("client_type", CLIENTS, ids=lambda client: client.__name__)
async def test_api_error_without_output_stays_rejected(client_type):
    error = await _observe_error(client_type, _error_payload(client_type, "empty"))
    assert error.delivery_state == "rejected"


@pytest.mark.parametrize("client_type", CLIENTS, ids=lambda client: client.__name__)
@pytest.mark.parametrize("status_code,expected", [(400, "rejected"), (503, "unknown")])
async def test_http_status_still_owns_delivery_classification(client_type, status_code, expected):
    error = await _observe_error(client_type, None, status_code=status_code)
    assert error.delivery_state == expected
    assert error.http_status == status_code


@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled"])
@pytest.mark.parametrize("output", [[], [COMPLETED_EFFECT]], ids=["empty", "completed_effect"])
async def test_responses_non_success_status_preserves_output_uncertainty(status, output):
    error = await _observe_error(OpenAIResponsesClient, {"status": status, "output": output})
    assert error.delivery_state == ("unknown" if output else "rejected")


@pytest.mark.parametrize(
    "client_type,payload,expected",
    [
        (
            OpenAICompatibleClient,
            {"error": "Synthetic failure", "choices": [{"message": {"tool_calls": [COMPLETED_EFFECT]}}]},
            "unknown",
        ),
        # Controller decision 1: output_text is ordinary text and must not disable fallback.
        (OpenAIResponsesClient, {"error": "Synthetic failure", "output_text": "Partial answer"}, "rejected"),
    ],
)
async def test_api_error_with_other_supported_output(client_type, payload, expected):
    error = await _observe_error(client_type, payload)
    assert error.delivery_state == expected


@pytest.mark.parametrize(
    "item_type",
    [
        "mcp_call",
        "web_search_call",
        "file_search_call",
        "code_interpreter_call",
        "image_generation_call",
    ],
)
async def test_responses_provider_executed_items_are_unknown(item_type):
    error = await _observe_error(OpenAIResponsesClient, {"error": "Synthetic failure", "output": [{"type": item_type}]})
    assert error.delivery_state == "unknown"


@pytest.mark.parametrize(
    "client_type,payload",
    [
        (OpenAICompatibleClient, {"error": "Synthetic failure", "choices": [None]}),
        (OpenAICompatibleClient, {"error": "Synthetic failure", "choices": [{"message": {"tool_calls": [None]}}]}),
        (OpenAIResponsesClient, {"error": "Synthetic failure", "output": [{"type": []}]}),
        (GeminiClient, {"error": "Synthetic failure", "candidates": [None]}),
        (GeminiClient, {"error": "Synthetic failure", "candidates": [{"content": [{"text": "Partial output"}]}]}),
        (AnthropicClient, {"type": "error", "error": "Synthetic failure", "content": [{"type": []}]}),
    ],
)
async def test_unreadable_api_error_kind_remains_an_llm_error(client_type, payload):
    error = await _observe_error(client_type, payload)
    assert error.delivery_state == "unknown"


@pytest.mark.parametrize(
    "client_type,payload",
    [
        (
            OpenAICompatibleClient,
            {
                "error": "Synthetic failure",
                "choices": [{"message": {"tool_calls": [{"type": "function", "function": "Not a function object"}]}}],
            },
        ),
        (OpenAIResponsesClient, {"error": "Synthetic failure", "output": [{"type": "message", "content": [None]}]}),
        (OpenAIResponsesClient, {"error": "Synthetic failure", "output_text": {"unexpected": "object"}}),
        (GeminiClient, {"error": "Synthetic failure", "candidates": [{"content": {"parts": [{"text": {}}]}}]}),
        (AnthropicClient, {"type": "error", "error": "Synthetic failure", "content": [{"type": "text", "text": {}}]}),
    ],
)
async def test_malformed_values_inside_non_effect_kind_are_rejected(client_type, payload):
    # Controller round 4: the readable non-effect kind decides; its field values cannot prove provider execution.
    error = await _observe_error(client_type, payload)
    assert error.delivery_state == "rejected"


@pytest.mark.parametrize(
    "client_type,output",
    [
        (
            OpenAIResponsesClient,
            {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "Synthetic refusal"}]}]},
        ),
        (
            GeminiClient,
            {
                "candidates": [
                    {"content": {"parts": [{"functionCall": {"name": "f", "args": {}}, "thoughtSignature": "sig"}]}}
                ]
            },
        ),
        (
            GeminiClient,
            {"candidates": [{"content": {"parts": [{"text": "Partial", "thought": True, "thoughtSignature": "sig"}]}}]},
        ),
        (OpenAIResponsesClient, {"output": [{"type": "mcp_list_tools"}]}),
        (
            OpenAIResponsesClient,
            {
                "output": [
                    {"type": "mcp_list_tools"},
                    {"type": "message", "content": [{"type": "output_text", "text": "Partial"}]},
                ]
            },
        ),
        (OpenAICompatibleClient, {"choices": [{"message": None}]}),
        (GeminiClient, {"candidates": [{"content": None}]}),
        (
            OpenAICompatibleClient,
            {
                "choices": [
                    {"message": {"tool_calls": [{"type": "function", "function": {"name": "f", "arguments": None}}]}}
                ]
            },
        ),
        (OpenAICompatibleClient, {"choices": [{"message": {"tool_calls": [{"type": "function", "function": None}]}}]}),
        (OpenAIResponsesClient, {"output": [{"type": "function_call", "name": "f", "arguments": None}]}),
        (AnthropicClient, {"content": [{"type": "tool_use", "name": "f", "input": None}]}),
        (GeminiClient, {"candidates": [{"content": {"parts": [{}]}}]}),
    ],
)
async def test_discovery_metadata_and_values_do_not_change_non_effect_kind(client_type, output):
    # MCP discovery and reasoning metadata carry no provider-side effect, even with null call values.
    error = await _observe_error(client_type, {"type": "error", "error": "Synthetic failure", **output})
    assert error.delivery_state == "rejected"


@pytest.mark.parametrize(
    "client_type,output",
    [
        (GeminiClient, {"candidates": [{"content": {"parts": []}, "urlContextMetadata": {"urlMetadata": []}}]}),
        (OpenAIResponsesClient, {"output": [{"type": "mcp_list_tools"}, COMPLETED_EFFECT]}),
    ],
)
async def test_provider_execution_is_not_hidden_by_discovery_or_empty_parts(client_type, output):
    error = await _observe_error(client_type, {"error": "Synthetic failure", **output})
    assert error.delivery_state == "unknown"


@pytest.mark.parametrize(
    "candidate",
    [
        {"groundingMetadata": {"webSearchQueries": ["synthetic"]}, "content": {"parts": []}},
        {"content": {"parts": [{"executableCode": {"language": "PYTHON", "code": "print(1)"}}]}},
    ],
)
async def test_gemini_provider_execution_without_text_is_unknown(candidate):
    error = await _observe_error(GeminiClient, {"error": "Synthetic failure", "candidates": [candidate]})
    assert error.delivery_state == "unknown"


@pytest.mark.parametrize("block_type", ["mcp_tool_use", "web_search_tool_result", "future_tool_result"])
async def test_anthropic_provider_tool_blocks_are_unknown(block_type):
    error = await _observe_error(
        AnthropicClient, {"type": "error", "error": "Synthetic failure", "content": [{"type": block_type}]}
    )
    assert error.delivery_state == "unknown"


async def test_anthropic_redacted_thinking_is_not_provider_execution():
    error = await _observe_error(
        AnthropicClient,
        {
            "type": "error",
            "error": "Synthetic failure",
            "content": [{"type": "redacted_thinking", "data": "synthetic-redacted-data"}],
        },
    )
    assert error.delivery_state == "rejected"


@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
async def test_compatible_success_with_null_error_returns_content(stream):
    requests = []

    def respond(request):
        requests.append(request)
        if stream:
            payload = {"error": None, "choices": [{"delta": {"content": "Synthetic success"}}]}
            return httpx.Response(200, text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json={"error": None, "choices": [{"message": {"content": "Synthetic success"}}]})

    client = OpenAICompatibleClient(api_key="synthetic-key", base_url="http://127.0.0.1/v1", model="synthetic")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        method = client.stream if stream else client.complete
        response = await method([LLMMessage(role="user", content="Synthetic task")], _http_max_attempts=1)
        assert response.content == "Synthetic success"
        assert len(requests) == 1
    finally:
        await client.close()
