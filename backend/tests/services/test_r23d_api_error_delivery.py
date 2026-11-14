"""An API error with provider output cannot prove rejection before execution."""

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


def _error_payload(client_type, output):
    data = {"error": {"message": "Synthetic provider failure"}}
    if client_type is OpenAICompatibleClient:
        data["choices"] = [{"message": {"content": output, "tool_calls": []}}]
    elif client_type is OpenAIResponsesClient:
        data.update(status="failed", output=output)
    elif client_type is GeminiClient:
        data["candidates"] = [{"content": {"parts": output}}]
    else:
        data.update(type="error", content=output)
    return data


async def _observe_error(client_type, payload, *, status_code=200):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status_code, json=payload)

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
    "output",
    [[COMPLETED_EFFECT], [{"type": "text", "text": "Partial output"}]],
    ids=["completed_effect", "partial_text"],
)
async def test_api_error_with_output_is_unknown(client_type, output):
    # Opaque items exercise each native output container without a tool-type allowlist.
    error = await _observe_error(client_type, _error_payload(client_type, output))
    assert error.delivery_state == "unknown"


@pytest.mark.parametrize("client_type", CLIENTS, ids=lambda client: client.__name__)
async def test_api_error_without_output_stays_rejected(client_type):
    error = await _observe_error(client_type, _error_payload(client_type, []))
    assert error.delivery_state == "rejected"


@pytest.mark.parametrize("client_type", CLIENTS, ids=lambda client: client.__name__)
@pytest.mark.parametrize("status_code,expected", [(400, "rejected"), (503, "unknown")])
async def test_http_status_still_owns_delivery_classification(client_type, status_code, expected):
    error = await _observe_error(client_type, _error_payload(client_type, [COMPLETED_EFFECT]), status_code=status_code)
    assert error.delivery_state == expected
    assert error.http_status == status_code


@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled"])
@pytest.mark.parametrize("output", [[], [COMPLETED_EFFECT]], ids=["empty", "completed_effect"])
async def test_responses_non_success_status_preserves_output_uncertainty(status, output):
    error = await _observe_error(OpenAIResponsesClient, {"status": status, "output": output})
    assert error.delivery_state == ("unknown" if output else "rejected")


@pytest.mark.parametrize(
    "client_type,payload",
    [
        (
            OpenAICompatibleClient,
            {"error": "Synthetic failure", "choices": [{"message": {"tool_calls": [COMPLETED_EFFECT]}}]},
        ),
        (OpenAIResponsesClient, {"error": "Synthetic failure", "output_text": "Partial answer"}),
    ],
)
async def test_api_error_with_other_supported_output_is_unknown(client_type, payload):
    error = await _observe_error(client_type, payload)
    assert error.delivery_state == "unknown"
