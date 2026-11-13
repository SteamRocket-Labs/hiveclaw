"""R20b round 3: the final empty-search reply is one bounded provider call."""

import json
from types import SimpleNamespace

import pytest

from app.kernel.contracts import TerminalReason
from app.services.llm_client import AnthropicClient, OpenAICompatibleClient
from app.services.runtime_display_status import EMPTY_SEARCH_FINAL_MESSAGE
from tests.kernel.test_r18a_empty_search_guard import (
    EMPTY_RESULT, _FakeClient, _answer, _kernel, _message_texts, _request, _round, _search_call,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [
    _answer(""),
    _round([{"id": "publish-again", "function": {
        "name": "plaza_create_post", "arguments": '{"content":"Duplicate"}'}}]),
    SimpleNamespace(content="Already done", tool_calls=[_search_call(6)],
                    reasoning_content=None, usage={"total_tokens": 5}),
])
async def test_invalid_summary_never_executes_tools_or_uses_the_empty_provider_exit(summary):
    executed = []

    async def execute(name, *_a, **_k):
        executed.append(name)
        return EMPTY_RESULT

    client = _FakeClient([*[_round([_search_call(i)]) for i in range(1, 6)], summary])
    result = await _kernel(client, execute).handle(_request())

    assert executed == ["search_company_kb"] * 5
    assert len(client.calls) == 6
    assert result.terminal_reason is TerminalReason.LOOP_GUARD
    assert result.failure_code == "repeated_empty_search"
    assert result.content == EMPTY_SEARCH_FINAL_MESSAGE and not result.failure_retryable


@pytest.mark.asyncio
async def test_summary_does_not_bind_or_drain_a_new_member_message_and_next_turn_has_tools():
    pending = []
    bound_rounds, drains = [], []

    async def execute(*_a, **_k):
        if len(client.calls) == 5:
            pending.append({"role": "user", "content": "Publish the new request"})
        return EMPTY_RESULT

    async def bind(index):
        bound_rounds.append(index)
        items = list(pending)
        pending.clear()
        return items

    async def drain():
        drains.append(True)
        return []

    client = _FakeClient([*[_round([_search_call(i)]) for i in range(1, 6)], _answer(EMPTY_SEARCH_FINAL_MESSAGE)])
    request = _request()
    request.round_input_bind, request.mid_run_message_drain = bind, drain
    result = await _kernel(client, execute).handle(request)
    assert result.terminal_reason is TerminalReason.TURN_STOP
    assert bound_rounds == [1, 2, 3, 4, 5] and len(drains) == 5
    assert "Publish the new request" not in _message_texts(client.calls[-1])
    assert pending == [{"role": "user", "content": "Publish the new request"}]
    assert result.final_tools

    next_client = _FakeClient([_answer("The new request was answered")])
    next_request = _request()
    next_request.messages = list(pending)
    next_result = await _kernel(next_client, execute).handle(next_request)
    assert next_client.calls[0]["tools"]
    assert "Publish the new request" in _message_texts(next_client.calls[0])
    assert next_result.content == "The new request was answered"


@pytest.mark.asyncio
async def test_summary_cannot_continue_for_a_stop_hook_or_receipt_verdict(monkeypatch):
    import app.kernel.engine as support

    async def stop(*_a, **_k):
        return SimpleNamespace(block=True, prevent_continuation=False, reason="Try again", stop_reason="")

    monkeypatch.setattr(support, "_emit_runtime_hook", stop)
    client = _FakeClient([*[_round([_search_call(i)]) for i in range(1, 6)], _answer(EMPTY_SEARCH_FINAL_MESSAGE)])
    request = _request()
    request.model_request_prepare = lambda **k: f"provider-{k['round_index']}"
    request.model_response_commit = lambda **k: {
        "provider_request_id": k["provider_request_id"], "continuation": {"verdict": "continue"}}
    result = await _kernel(client, lambda *_a, **_k: EMPTY_RESULT).handle(request)
    assert len(client.calls) == 6
    assert result.content == EMPTY_SEARCH_FINAL_MESSAGE and result.terminal_reason is TerminalReason.TURN_STOP


@pytest.mark.asyncio
async def test_guard_does_not_start_an_auxiliary_compaction_provider_call(monkeypatch):
    import app.kernel.engine as support

    executed, compression_sites = [], []

    async def execute(*_a, **_k):
        executed.append(True)
        return json.dumps({"status": "invalid_request"}) if len(executed) == 1 else EMPTY_RESULT

    async def compress(_compressor, messages, **kwargs):
        compression_sites.append((len(executed), kwargs["instructions"]))
        return messages

    async def preflight(*, messages, compress_messages, **_kwargs):
        await compress_messages(support._llm_messages_to_dicts(messages))
        return SimpleNamespace(changed=False)

    monkeypatch.setattr(support, "_compress_messages_with_lifecycle_hooks", compress)
    monkeypatch.setattr(support, "prepare_session_context_for_request", preflight)
    client = _FakeClient([*[_round([_search_call(i)]) for i in range(1, 7)], _answer(EMPTY_SEARCH_FINAL_MESSAGE)])
    result = await _kernel(client, execute).handle(_request())
    # R20b round 3: malformed + five empty results reach the periodic-compaction boundary.
    assert len(executed) == 6 and len(client.calls) == 7
    assert result.content == EMPTY_SEARCH_FINAL_MESSAGE
    assert compression_sites and not [site for site in compression_sites if site[0] >= 6]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["HTTP 400 context length exceeded", "HTTP 503 unavailable"])
async def test_summary_model_error_cannot_retry_compression_or_the_fallback_model(error):
    from app.services.llm_client import LLMError

    class Client(_FakeClient):
        async def stream(self, **kwargs):
            if len(self.calls) == 5:
                self.calls.append(kwargs)
                raise LLMError(error, delivery_state="rejected")
            return await super().stream(**kwargs)

    client = Client([_round([_search_call(i)]) for i in range(1, 6)])
    request = _request()
    request.fallback_model = SimpleNamespace(provider="openai", model="fallback")
    result = await _kernel(client, lambda *_a, **_k: EMPTY_RESULT).handle(request)
    assert len(client.calls) == 6
    assert result.terminal_reason is TerminalReason.PROVIDER_ERROR
    assert result.failure_code and result.failure_code != "repeated_empty_search"


@pytest.mark.asyncio
async def test_member_stop_during_the_summary_uses_the_cancel_path():
    import asyncio

    started, cancel = asyncio.Event(), asyncio.Event()

    class Client(_FakeClient):
        async def stream(self, **kwargs):
            if len(self.calls) == 5:
                self.calls.append(kwargs)
                started.set()
                await asyncio.Event().wait()
            return await super().stream(**kwargs)

    client = Client([_round([_search_call(i)]) for i in range(1, 6)])
    request = _request()
    request.cancel_event = cancel
    task = asyncio.create_task(_kernel(client, lambda *_a, **_k: EMPTY_RESULT).handle(request))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        cancel.set()
        result = await asyncio.wait_for(task, timeout=10)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(client.calls) == 6 and result.terminal_reason is TerminalReason.USER_CANCEL


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "minimax"])
@pytest.mark.parametrize("summary_outcome", ["stop", "output_cap", "error"])
async def test_summary_protocol_payload_is_scoped_and_sent_once(provider, summary_outcome):
    payloads = []

    class Response:
        headers = {}

        @property
        def status_code(self):
            return 503 if len(payloads) == 6 and summary_outcome == "error" else 200

        async def aiter_bytes(self):
            yield b"provider unavailable"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def aiter_lines(self):
            index = len(payloads)
            if provider == "anthropic":
                blocks = ([{"type": "content_block_start", "index": 0, "content_block": {
                    "type": "tool_use", "id": f"search-{index}", "name": "search_company_kb", "input": {}}},
                    {"type": "content_block_delta", "index": 0, "delta": {
                        "type": "input_json_delta", "partial_json": json.dumps({"query": f"question {index}"})}}]
                    if index < 6 else [{"type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": EMPTY_SEARCH_FINAL_MESSAGE}}])
                blocks.append({"type": "message_delta", "delta": {
                    "stop_reason": "tool_use" if index < 6 else ("max_tokens" if summary_outcome == "output_cap" else "end_turn")}, "usage": {"output_tokens": 5}})
                for block in blocks:
                    yield "event: " + block["type"]
                    yield "data: " + json.dumps(block)
            else:
                delta = ({"tool_calls": [{"index": 0, **_search_call(index)}]}
                    if index < 6 else {"content": EMPTY_SEARCH_FINAL_MESSAGE})
                yield "data: " + json.dumps({"choices": [{"delta": delta,
                    "finish_reason": "tool_calls" if index < 6 else ("length" if summary_outcome == "output_cap" else "stop")}],
                    "usage": {"total_tokens": 5}})
                yield "data: [DONE]"

    class Transport:
        def stream(self, *_a, **kwargs):
            payloads.append(kwargs["json"])
            assert len(payloads) <= 6, "The final summary must not retry or continue after an output cap"
            return Response()

    client = (AnthropicClient(api_key="local", model="claude-local") if provider == "anthropic"
        else OpenAICompatibleClient(api_key="local", model="MiniMax-local", provider="minimax"))

    async def transport():
        return Transport()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(client, "_get_client", transport)
    try:
        request = _request()
        request.model.provider = provider
        result = await _kernel(client, lambda *_a, **_k: EMPTY_RESULT).handle(request)
    finally:
        monkeypatch.undo()

    if summary_outcome == "error":
        assert result.terminal_reason is TerminalReason.PROVIDER_ERROR
    else:
        assert result.content == EMPTY_SEARCH_FINAL_MESSAGE
    assert len(payloads) == 6
    assert all(payload["tools"] for payload in payloads[:5])
    summary = payloads[-1]
    if provider == "anthropic":
        assert summary["tools"] == payloads[-2]["tools"]
        assert summary["tool_choice"] == {"type": "none"}
        assert any(block.get("type") == "tool_result" for message in summary["messages"]
            if isinstance(message["content"], list) for block in message["content"])
    else:
        assert "tools" not in summary and "tool_choice" not in summary
    assert "_http_max_attempts" not in summary
