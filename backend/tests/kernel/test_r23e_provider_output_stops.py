"""Provider execution fences continuations and preserves the typed failure boundary."""

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app.kernel.contracts import ProviderRequestNeedsReconciliation, TerminalReason
from app.services.llm_client import LLMMessage, LLMResponse, OpenAICompatibleClient, OpenAIResponsesClient
from tests.kernel.test_r18a_empty_search_guard import EMPTY_RESULT, _FakeClient, _kernel, _request, _round, _search_call
from tests.services.test_r23e_provider_output import MCP_CALL, success_body


@pytest.mark.parametrize("stage", ["initial", "initial_with_text", "continuation"])
async def test_output_cap_never_continues_after_provider_output(stage):
    from app.kernel.engine import _continue_after_output_cap

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) > (2 if stage == "continuation" else 1):
            return httpx.Response(200, json=success_body("responses", text=True))
        items = [] if stage == "continuation" and len(requests) == 1 else [MCP_CALL]
        body = success_body("responses", items, text=stage == "initial_with_text")
        body.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        return httpx.Response(200, json=body)

    client = OpenAIResponsesClient(api_key="synthetic", model="synthetic", base_url="http://127.0.0.1")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    messages = [LLMMessage(role="user", content="Synthetic task")]
    options = {"mcp_servers": [{"name": "synthetic", "url": "http://127.0.0.1:1/mcp"}]}
    try:
        response = await client.stream(messages, **options)
        merged, receipts = await _continue_after_output_cap(
            client=client, response=response, stream_messages=messages, cancel_event=None,
            active_model=SimpleNamespace(provider="openai-response", model="synthetic"),
            on_chunk=None, on_thinking=None, reasoning_kwargs=options,
        )
        assert len(requests) == (2 if stage == "continuation" else 1), "No provider request after observed execution"
        assert len(receipts) == (1 if stage == "continuation" else 0)
        assert merged.provider_output_seen is True and merged.tool_calls == []
        assert merged.content == ("Synthetic answer" if stage == "initial_with_text" else "")
        assert merged.finish_reason == "length"  # Text plus execution is intentionally delivered truncated.
        assert all(request["mcp_servers"] == options["mcp_servers"] for request in requests)
    finally:
        await client.close()


@pytest.mark.parametrize("settlement", ["needs_reconciliation", None], ids=["ledger_hold", "undecided"])
async def test_empty_provider_result_raises_after_recording_tokens_without_fallback(settlement):
    failures, charged = [], []
    response = LLMResponse(content="", provider_output_seen=True, usage={"total_tokens": 3}, finish_reason="stop")
    client = _FakeClient([response, LLMResponse(content="Fallback must not run", usage={"total_tokens": 1})])
    kernel = _kernel(client, lambda *_a, **_k: "")
    kernel._deps = replace(kernel._deps, record_token_usage=lambda *args: charged.append(args))
    request = _request()
    request.fallback_model = SimpleNamespace(provider="openai", model="fallback")
    request.model_request_prepare = lambda **_k: "provider-r23e"

    def fail(**payload):
        failures.append(payload)
        return settlement

    request.model_request_fail = fail
    with pytest.raises(ProviderRequestNeedsReconciliation) as caught:
        await kernel.handle(request)
    assert caught.value.provider_request_id == "provider-r23e"
    assert caught.value.error_class == "provider_empty_response"
    assert failures == [{"round_index": 1, "provider_request_id": "provider-r23e",
                         "error_class": "provider_empty_response", "delivery_state": "response_received", "retry_safe": False}]
    assert charged == [(request.agent_id, 3)] and len(client.calls) == 1


async def test_discarded_empty_search_summary_is_not_retry_safe_after_provider_output():
    failures, executed = [], []
    client = _FakeClient([*[_round([_search_call(i)]) for i in range(1, 6)],
                         LLMResponse(content="", provider_output_seen=True, usage={"total_tokens": 3})])
    request = _request()
    request.model.provider_options = {"mcp_servers": [{"name": "synthetic"}]}
    request.model_request_prepare = lambda **p: f"provider-{p['round_index']}"
    request.model_request_fail = lambda **p: failures.append(p)
    result = await _kernel(client, lambda name, *_a, **_k: (executed.append(name), EMPTY_RESULT)[1]).handle(request)
    assert failures == [{"round_index": 6, "provider_request_id": "provider-6", "continuation_index": 0,
                         "error_class": "repeated_empty_search", "delivery_state": "response_received", "retry_safe": False}]
    assert len(client.calls) == 6 and executed == ["search_company_kb"] * 5
    assert result.terminal_reason is TerminalReason.LOOP_GUARD and not result.failure_retryable


async def test_all_choice_zero_provider_indices_are_fenced_before_first_item_projection():
    requests, projected, executed = [], [], []
    frames = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "remote-0", "type": "mcp_call"},
            {"index": 1, "id": "remote-1", "type": "mcp_call"},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "remote-1", "output": "Synthetic completion"}]},
                      "finish_reason": "stop"}]},
    ]

    def respond(request):
        requests.append(json.loads(request.content))
        body = frames if len(requests) == 1 else [{"choices": [{"delta": {"content": "Unexpected second send"}}]}]
        usage = {"choices": [], "usage": {"total_tokens": 1}}
        wire = "".join(f"data: {json.dumps(part)}\n\n" for part in [*body, usage]) + "data: [DONE]\n\n"
        return httpx.Response(200, text=wire)

    client = OpenAICompatibleClient(api_key="synthetic", model="local", base_url="http://127.0.0.1")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    real_stream = client.stream

    async def observe(**kwargs):
        response = await real_stream(**kwargs)
        projected.append(response)
        return response

    client.stream = observe
    request = _request()
    request.model.provider_options = {"mcp_servers": [{"name": "synthetic", "url": "http://127.0.0.1:1/mcp"}]}

    def execute(name, *_a, **_k):
        executed.append(name)
        return '{"status":"unavailable"}'

    result = await _kernel(client, execute).handle(request)
    assert len(requests) == 1, "No second provider request after provider-only output"
    assert executed == [] and projected[0].tool_calls == []
    assert projected[0].provider_output_seen is True
    assert result.terminal_reason is TerminalReason.PROVIDER_ERROR
    assert requests[0]["mcp_servers"] == request.model.provider_options["mcp_servers"]

    # The same parser still collects a function at index 0 and its type-less continuation.
    control_frames = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "local-0", "type": "function",
                      "function": {"name": "local", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]},
                      "finish_reason": "tool_calls"}]},
    ]
    control_wire = "".join(f"data: {json.dumps(part)}\n\n" for part in control_frames) + "data: [DONE]\n\n"
    control_requests = []

    def control_transport(request):
        control_requests.append(request)
        return httpx.Response(200, text=control_wire)

    control_client = OpenAICompatibleClient(api_key="synthetic", model="local", base_url="http://127.0.0.1")
    control_client._client = httpx.AsyncClient(transport=httpx.MockTransport(control_transport))
    try:
        control = await control_client.stream([LLMMessage(role="user", content="Synthetic control")])
        assert control.tool_calls == [{"id": "local-0", "type": "function",
                                       "function": {"name": "local", "arguments": "{}"}}]
        assert control.provider_output_seen is False and len(control_requests) == 1
        assert executed == []
    finally:
        await control_client.close()
