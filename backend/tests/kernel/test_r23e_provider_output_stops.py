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


@pytest.mark.parametrize("settlement", ["needs_reconciliation", None, "failed"],
                         ids=["ledger_hold", "undecided", "effect_free_failed"])
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
    if settlement == "failed":
        # R23e S1: the trusted effect-free settlement stays a plain provider failure.
        result = await kernel.handle(request)
        assert result.terminal_reason is TerminalReason.PROVIDER_ERROR
    else:
        with pytest.raises(ProviderRequestNeedsReconciliation) as caught:
            await kernel.handle(request)
        assert caught.value.provider_request_id == "provider-r23e"
        assert caught.value.error_class == "provider_empty_response"
    assert failures == [{"round_index": 1, "provider_request_id": "provider-r23e",
                         "error_class": "provider_empty_response", "delivery_state": "response_received", "retry_safe": False}]
    assert charged == [(request.agent_id, 3)] and len(client.calls) == 1


@pytest.mark.parametrize("settlement", ["needs_reconciliation", "failed"])
async def test_discarded_empty_search_summary_is_not_retry_safe_after_provider_output(settlement):
    failures, executed, charged = [], [], []
    searches = [_round([_search_call(i)]) for i in range(1, 6)]
    for response in searches:
        response.usage = {"total_tokens": 0}  # Isolate the summary's single token charge.
    client = _FakeClient([*searches, LLMResponse(content="", provider_output_seen=True, usage={"total_tokens": 3})])
    request = _request()
    request.model.provider_options = {"mcp_servers": [{"name": "synthetic"}]}
    request.model_request_prepare = lambda **p: f"provider-{p['round_index']}"
    request.model_request_fail = lambda **p: (failures.append(p), settlement)[1]
    kernel = _kernel(client, lambda name, *_a, **_k: (executed.append(name), EMPTY_RESULT)[1])
    kernel._deps = replace(kernel._deps, record_token_usage=lambda *args, **_k: charged.append(args))
    if settlement == "failed":
        result = await kernel.handle(request)
        assert result.terminal_reason is TerminalReason.LOOP_GUARD and not result.failure_retryable
    else:
        # R23e N1: preserve the hold only after accounting; no empty-search answer escapes to delegation.
        with pytest.raises(ProviderRequestNeedsReconciliation) as caught:
            await kernel.handle(request)
        assert caught.value.provider_request_id == "provider-6"
        assert caught.value.error_class == "provider_empty_response"
    assert failures == [{"round_index": 6, "provider_request_id": "provider-6", "continuation_index": 0,
                         "error_class": "repeated_empty_search", "delivery_state": "response_received", "retry_safe": False}]
    assert len(client.calls) == 6 and executed == ["search_company_kb"] * 5
    assert charged == [(request.agent_id, 3)]


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


async def test_selected_untyped_function_survives_same_chunk_provider_type_and_text_still_completes():
    from app.tools.result_envelope import ToolContentEnvelope

    requests, projected, executed = [], [], []
    local = {"index": 0, "id": "local-0", "function": {"name": "local", "arguments": "{}"}}
    body = {"choices": [{"delta": {"tool_calls": [local, {"index": 0, "type": "mcp_call"}]},
                         "finish_reason": "tool_calls"}], "usage": {"total_tokens": 1}}

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n")

    client = OpenAICompatibleClient(api_key="synthetic", model="local", base_url="http://127.0.0.1")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    real_stream = client.stream

    async def observe(**kwargs):
        response = await real_stream(**kwargs)
        projected.append(response)
        return response

    client.stream = observe

    def execute(name, *_a, **_k):
        executed.append(name)
        # Use the existing trusted-tool terminal contract, not a round cap, to end on this tool round.
        return ToolContentEnvelope(text="Local tool finished", terminal_signal="waiting_for_user")

    result = await _kernel(client, execute, tools=("local",)).handle(_request())
    assert projected[0].tool_calls == [{"id": "local-0", "type": "function", "function": local["function"]}]
    assert projected[0].provider_output_seen is True and executed == ["local"] and len(requests) == 1
    assert result.terminal_reason is TerminalReason.TURN_STOP and result.tool_terminal_signal == "waiting_for_user"

    # Null on an unselected index must not lock it to a non-function kind before a later function delta.
    first = {"choices": [{"delta": {"tool_calls": [
        {**local, "type": "function"}, {"index": 1, "type": None},
    ]}}]}
    second = {"index": 1, "id": "local-1", "type": "function", "function": {"name": "next", "arguments": "{}"}}
    frames = [first, {"choices": [{"delta": {"tool_calls": [second]}}]}]
    wire = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, text=wire)))
    try:
        null_secondary = await real_stream([LLMMessage(role="user", content="Synthetic null continuation")])
        assert null_secondary.provider_output_seen is False
        assert null_secondary.tool_calls == [
            {"id": "local-0", "type": "function", "function": local["function"]},
            {"id": "local-1", "type": "function", "function": second["function"]},
        ]
    finally:
        await client.close()

    # Grok nit: the ordinary text+provider-output branch must also traverse the real kernel, without a database.
    text_client = _FakeClient([LLMResponse(content="Synthetic answer", provider_output_seen=True,
                                         usage={"total_tokens": 1}, finish_reason="stop")])
    text_result = await _kernel(text_client, lambda *_a, **_k: pytest.fail("No client tool is requested")).handle(_request())
    assert text_result.content == "Synthetic answer" and text_result.terminal_reason is TerminalReason.TURN_STOP
    assert len(text_client.calls) == 1
