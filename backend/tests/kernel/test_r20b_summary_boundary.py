"""R20b round 4: one tool-free summary round, then normal pending-input continuation."""

import json
from types import SimpleNamespace

import pytest

from app.kernel.contracts import TerminalReason
from app.services.llm_client import AnthropicClient, OpenAICompatibleClient
from app.services.runtime_display_status import EMPTY_SEARCH_FINAL_MESSAGE, EMPTY_SEARCH_MATERIAL_FOUND_MESSAGE
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
@pytest.mark.parametrize("search_again", [False, True])
async def test_pending_input_continues_with_tools_and_cannot_start_a_second_summary(search_again):
    pending, executed, bound_rounds, drains, events = [], [], [], [], []
    new_request = "Answer the new question"

    async def execute(name, *_a, **_k):
        executed.append(name)
        if len(executed) == 5:
            pending.append({"role": "user", "content": new_request})
        return EMPTY_RESULT

    async def bind(index):
        bound_rounds.append(index)
        items = list(pending)
        pending.clear()
        return items

    async def drain():
        drains.append(True)
        return []

    def commit(**payload):
        return {"provider_request_id": payload["provider_request_id"], "continuation": {
            "verdict": "continue" if payload["round_index"] == 6 else "terminal_candidate",
            "pending_obligations": [{"kind": "pending_input"}] if payload["round_index"] == 6 else []}}

    continued = _round([_search_call(6)]) if search_again else _answer("The new question was answered")
    client = _FakeClient([*[_round([_search_call(i)]) for i in range(1, 6)],
        _answer(EMPTY_SEARCH_FINAL_MESSAGE), continued])
    request = _request()
    request.round_input_bind, request.mid_run_message_drain = bind, drain
    request.model_request_prepare = lambda **k: f"provider-{k['round_index']}"
    request.model_response_commit = commit
    request.on_event = lambda event: events.append(event)
    result = await _kernel(client, execute).handle(request)
    # R20b round 4/I3': the summary skips input; round seven resumes the normal tool surface.
    assert len(client.calls) == 7
    assert bound_rounds == [1, 2, 3, 4, 5, 7] and len(drains) == 6
    assert client.calls[5]["tools"] is None and client.calls[6]["tools"] == client.calls[0]["tools"]
    assert "will not run" in _message_texts(client.calls[5])
    # R20b round 5/SF2A: only the summary request receives the final-reply instruction.
    assert "will not run" not in _message_texts(client.calls[6])
    assert "final reply" not in _message_texts(client.calls[6])
    assert new_request not in _message_texts(client.calls[5])
    assert new_request in _message_texts(client.calls[6]) and pending == []
    assert result.final_tools
    # R20b round 4/I1': a later guard abort never gets another summary round.
    assert sum(event.get("part", {}).get("event_type") == "loop_guard_terminal_evidence" for event in events) == 1
    if search_again:
        assert executed == ["search_company_kb"] * 6
        # R20b round 5/nit 3: the retained count aborts on exactly one further empty search.
        assert len(executed) - 5 == 1
        assert result.terminal_reason is TerminalReason.LOOP_GUARD
        assert result.failure_code == "repeated_empty_search" and not result.failure_retryable
        assert result.content == EMPTY_SEARCH_FINAL_MESSAGE
    else:
        assert executed == ["search_company_kb"] * 5
        assert result.terminal_reason is TerminalReason.TURN_STOP
        assert result.content == "The new question was answered"


@pytest.mark.asyncio
@pytest.mark.parametrize("hit_first", [False, True])
async def test_second_empty_search_stop_selects_its_sentence_after_all_round_results(hit_first):
    """R21a r3 S3: both orders persist the exit-time material sentence."""
    pending, executed, bound_rounds, persisted, events = [], [], [], [], []
    new_request = "Use the new material to answer"

    class Client(_FakeClient):
        async def stream(self, **kwargs):
            response = await super().stream(**kwargs)
            if len(self.calls) == 6:
                pending.append({"role": "user", "content": new_request})
            return response

    async def execute(name, *_a, **_k):
        executed.append(name)
        return '{"status":"ok","results":[{"text":"New weekly report material"}]}' if (
            name == "search_memory") else EMPTY_RESULT

    async def bind(index):
        bound_rounds.append(index)
        items = list(pending)
        pending.clear()
        return items

    def commit(**payload):
        return {"provider_request_id": payload["provider_request_id"], "continuation": {
            "verdict": "continue" if payload["round_index"] == 6 else "terminal_candidate",
            "pending_obligations": [{"kind": "pending_input"}] if payload["round_index"] == 6 else []}}

    async def persist(_request, _config, content, _messages):
        persisted.append(content)

    memory_call = {"id": "memory-hit", "function": {
        "name": "search_memory", "arguments": '{"query":"weekly report"}'}}
    pair = [memory_call, _search_call(6)] if hit_first else [_search_call(6), memory_call]
    client = Client([*[_round([_search_call(i)]) for i in range(1, 6)],
        _answer(EMPTY_SEARCH_FINAL_MESSAGE), _round(pair)])
    kernel = _kernel(client, execute, tools=("search_company_kb", "search_memory"))
    kernel._persist_before_exit = persist
    request = _request()
    request.round_input_bind = bind
    request.model_request_prepare = lambda **p: f"provider-{p['round_index']}"
    request.model_response_commit = commit
    request.on_event = events.append
    result = await kernel.handle(request)

    assert len(client.calls) == 7 and len(executed) == 7
    assert executed.count("search_company_kb") == 6 and executed.count("search_memory") == 1
    assert bound_rounds == [1, 2, 3, 4, 5, 7] and pending == []
    assert client.calls[5]["tools"] is None and client.calls[6]["tools"] == client.calls[0]["tools"]
    assert new_request not in _message_texts(client.calls[5])
    assert new_request in _message_texts(client.calls[6])
    assert sum(event.get("part", {}).get("event_type") == "loop_guard_terminal_evidence" for event in events) == 1
    assert result.terminal_reason is TerminalReason.LOOP_GUARD
    assert result.failure_code == "repeated_empty_search" and result.failure_retryable is False
    assert result.content == EMPTY_SEARCH_MATERIAL_FOUND_MESSAGE
    assert persisted == [result.content]


@pytest.mark.asyncio
async def test_summary_skips_stop_hook_continuation(monkeypatch):
    import app.kernel.engine as support

    stops = []

    async def stop(event, *_a, **_k):
        if event == "stop":
            stops.append(True)
        return SimpleNamespace(block=True, prevent_continuation=False, reason="Try again", stop_reason="")

    monkeypatch.setattr(support, "_emit_runtime_hook", stop)
    client = _FakeClient([*[_round([_search_call(i)]) for i in range(1, 6)], _answer(EMPTY_SEARCH_FINAL_MESSAGE)])
    result = await _kernel(client, lambda *_a, **_k: EMPTY_RESULT).handle(_request())
    assert len(client.calls) == 6 and stops == []
    assert result.content == EMPTY_SEARCH_FINAL_MESSAGE and result.terminal_reason is TerminalReason.TURN_STOP


@pytest.mark.asyncio
async def test_summary_uses_normal_preflight_and_periodic_compaction(monkeypatch):
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
    assert len(executed) == 6 and len(client.calls) == 7
    assert result.content == EMPTY_SEARCH_FINAL_MESSAGE
    # R20b round 4/I1' restores normal round machinery; the old compression ban no longer applies.
    assert (6, "mid_loop_context_compaction") in compression_sites
    assert (6, "request_preflight_context_compaction") in compression_sites


@pytest.mark.asyncio
async def test_summary_fallback_stays_tool_free_and_discards_its_tool_call():
    from app.services.llm_client import LLMError

    class Client(_FakeClient):
        async def stream(self, **kwargs):
            if len(self.calls) == 5:
                self.calls.append(kwargs)
                raise LLMError("HTTP 503 temporarily unavailable", delivery_state="rejected", http_status=503)
            return await super().stream(**kwargs)

    client = Client([*[_round([_search_call(i)]) for i in range(1, 6)], _round([{
        "id": "fallback-must-not-publish", "function": {
            "name": "plaza_create_post", "arguments": '{"content":"Duplicate"}'}}])])
    executed, events = [], []

    async def execute(name, *_a, **_k):
        executed.append(name)
        return EMPTY_RESULT

    request = _request()
    request.fallback_model = SimpleNamespace(provider="openai", model="fallback")
    request.on_event = lambda event: events.append(event)
    result = await _kernel(client, execute).handle(request)
    # R20b round 4/I1': fallback is inside the summary round, with the same tool isolation.
    assert len(client.calls) == 7 and all(call["tools"] is None for call in client.calls[5:])
    assert any(event.get("type") == "runtime_fallback" for event in events)
    assert executed == ["search_company_kb"] * 5
    assert result.terminal_reason is TerminalReason.LOOP_GUARD
    assert result.failure_code == "repeated_empty_search" and not result.failure_retryable


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
@pytest.mark.parametrize("summary_outcome", ["stop", "output_cap", "retry"])
async def test_summary_protocol_payload_is_scoped_across_normal_retries_and_continuations(provider, summary_outcome):
    payloads, prepared = [], []

    class Response:
        headers = {"retry-after": "0"}

        @property
        def status_code(self):
            return 429 if len(payloads) == 6 and summary_outcome == "retry" else 200

        async def aiter_bytes(self):
            yield b"rate limited"

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
                        "delta": {"type": "text_delta", "text": (EMPTY_SEARCH_FINAL_MESSAGE if index == 6 or summary_outcome == "retry"
                            else "摘要已完成。")}}])
                blocks.append({"type": "message_delta", "delta": {
                    "stop_reason": "tool_use" if index < 6 else ("max_tokens" if summary_outcome == "output_cap" and index == 6 else "end_turn")}, "usage": {"output_tokens": 5}})
                for block in blocks:
                    yield "event: " + block["type"]
                    yield "data: " + json.dumps(block)
            else:
                delta = ({"tool_calls": [{"index": 0, **_search_call(index)}]}
                    if index < 6 else {"content": (EMPTY_SEARCH_FINAL_MESSAGE if index == 6 or summary_outcome == "retry"
                        else "摘要已完成。")})
                yield "data: " + json.dumps({"choices": [{"delta": delta,
                    "finish_reason": "tool_calls" if index < 6 else ("length" if summary_outcome == "output_cap" and index == 6 else "stop")}],
                    "usage": {"total_tokens": 5}})
                yield "data: [DONE]"

    class Transport:
        def stream(self, *_a, **kwargs):
            payloads.append(kwargs["json"])
            assert len(payloads) <= 7, "The scripted summary must finish within one retry or continuation"
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
        request.model_request_prepare = lambda **p: prepared.append(p) or f"provider-{len(prepared)}"
        result = await _kernel(client, lambda *_a, **_k: EMPTY_RESULT).handle(request)
    finally:
        monkeypatch.undo()

    assert result.terminal_reason is TerminalReason.TURN_STOP
    assert result.content == EMPTY_SEARCH_FINAL_MESSAGE + ("摘要已完成。" if summary_outcome == "output_cap" else "")
    # R20b round 4/I1' allows the normal physical retry or output-cap continuation.
    assert len(payloads) == (6 if summary_outcome == "stop" else 7)
    assert all(payload["tools"] for payload in payloads[:5])
    for summary in payloads[5:]:
        if provider == "anthropic":
            assert summary["tools"] == payloads[4]["tools"]
            assert summary["tool_choice"] == {"type": "none"}
            assert any(block.get("type") == "tool_result" for message in summary["messages"]
                if isinstance(message["content"], list) for block in message["content"])
        else:
            assert "tools" not in summary and "tool_choice" not in summary
        assert "_http_max_attempts" not in summary
    for snapshot in [p for p in prepared if p["round_index"] == 6]:
        if provider == "anthropic":
            assert snapshot["tools"] == prepared[0]["tools"]
            assert snapshot["wire_request"]["tools"] == prepared[0]["tools"]
            assert snapshot["wire_request"]["reasoning"]["tool_choice"] == {"type": "none"}
        else:
            assert not snapshot["tools"] and snapshot["wire_request"]["tools"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("search_first", [False, True])
async def test_read_loop_abort_keeps_priority_over_same_round_empty_search(monkeypatch, parallel, search_first):
    import app.kernel.engine as support
    from app.services.runtime_display_status import runtime_failure_observation

    task_id = "11111111-2222-4333-8444-555555555555"
    executed, events = [], []
    terminal = {"task_id": task_id, "task_type": "delegation",
        **runtime_failure_observation("failed", {"error_code": "provider_error"})}

    async def execute(name, *_a, **_k):
        executed.append(name)
        return EMPTY_RESULT if name == "search_company_kb" else json.dumps(terminal)

    monkeypatch.setattr(support, "_is_concurrency_safe_tool", lambda _name: parallel)
    rounds = []
    for index in range(1, 6):
        read = {"id": f"read-{index}", "function": {
            "name": "check_async_task", "arguments": json.dumps({"task_id": task_id})}}
        calls = [_search_call(index), read] if search_first else [read, _search_call(index)]
        rounds.append(_round(calls))
    client = _FakeClient([*rounds, _answer("An empty-search summary must not replace the read failure")])
    request = _request()
    request.on_event = lambda event: events.append(event)
    result = await _kernel(client, execute, tools=("search_company_kb", "check_async_task")).handle(request)
    assert result.terminal_reason is TerminalReason.LOOP_GUARD
    assert result.failure_code == "identical_terminal_task_read"
    assert len(client.calls) == 5 and len(executed) == 10
    assert not any(event.get("part", {}).get("event_type") == "loop_guard_terminal_evidence" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("max_rounds,outcome", [(6, "text"), (5, "text"), (6, "tool")])
async def test_pending_input_gets_one_normal_continuation_at_the_round_cap(max_rounds, outcome):
    pending, bound, executed, persisted = [], [], [], []
    new_request, new_answer = "Read the new member's file", "The new question was answered"

    async def execute(name, *_a, **_k):
        executed.append(name)
        if len(executed) == 5:
            pending.append({"role": "user", "content": new_request})
        return EMPTY_RESULT if name == "search_company_kb" else "New file contents"

    async def bind(index):
        bound.append(index)
        items = list(pending)
        pending.clear()
        return items

    def commit(**payload):
        content = payload["response"].get("content")
        if content:
            persisted.append({"role": "assistant", "content": content})
        return {"provider_request_id": payload["provider_request_id"], "continuation": {
            "verdict": "continue" if payload["round_index"] == 6 else "terminal_candidate",
            "pending_obligations": [{"kind": "pending_input"}] if payload["round_index"] == 6 else []}}

    continued = _answer(new_answer) if outcome == "text" else _round([{
        "id": "read-new-file", "function": {"name": "read_file", "arguments": '{"path":"workspace/new.md"}'}}])
    client = _FakeClient([*[_round([_search_call(i)]) for i in range(1, 6)],
        _answer(EMPTY_SEARCH_FINAL_MESSAGE), continued, _answer("No further round is allowed")])
    request = _request()
    request.round_input_bind = bind
    request.model_request_prepare = lambda **p: f"provider-{p['round_index']}"
    request.model_response_commit = commit
    result = await _kernel(client, execute, max_tool_rounds=max_rounds,
        tools=("search_company_kb", "read_file")).handle(request)
    # R20b round 5/SF3: the one-shot slot covers summaries at M and M+1.
    assert bound == [1, 2, 3, 4, 5, 7] and pending == []
    assert len(client.calls) == 7
    assert client.calls[5]["tools"] is None and client.calls[6]["tools"] == client.calls[0]["tools"]
    assert new_request not in _message_texts(client.calls[5])
    assert new_request in _message_texts(client.calls[6])
    if outcome == "text":
        assert result.terminal_reason is TerminalReason.TURN_STOP
        assert result.failure_code is None and result.content == new_answer
        assert persisted == [{"role": "assistant", "content": EMPTY_SEARCH_FINAL_MESSAGE},
            {"role": "assistant", "content": new_answer}]
        assert executed == ["search_company_kb"] * 5
    else:
        assert result.terminal_reason is TerminalReason.TOOL_BUDGET
        assert result.failure_code == "tool_round_limit"
        assert executed == ["search_company_kb"] * 5 + ["read_file"]


@pytest.mark.asyncio
@pytest.mark.parametrize("generic_guard", [False, True])
async def test_extra_range_slot_requires_the_summary_input_continuation_flag(monkeypatch, generic_guard):
    import app.kernel.engine as support
    from app.kernel.loop_guard import LoopGuardDecision

    executed = []
    observe = support.LoopGuard.observe_tool_result

    async def execute(name, *_a, **_k):
        executed.append(name)
        return '{"status":"ok","results":[{"id":"found"}]}'

    def observe_result(self, *args, **kwargs):
        decision = observe(self, *args, **kwargs)
        if generic_guard and len(executed) >= 2:
            return LoopGuardDecision(reason="repeated_tool_failure", message="No progress",
                trace_event={"reason": "repeated_tool_failure"})
        return decision

    monkeypatch.setattr(support.LoopGuard, "observe_tool_result", observe_result)
    client = _FakeClient([_round([_search_call(i)]) for i in range(1, 5)])
    result = await _kernel(client, execute, max_tool_rounds=2).handle(_request())
    # R20b round 5/SF3: even a sticky generic summary flag only retains its original single extra slot.
    assert len(client.calls) == len(executed) == 2 + int(generic_guard)
    assert result.terminal_reason is TerminalReason.TOOL_BUDGET


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ["model_request_fail", "persist"])
async def test_discard_settlement_errors_propagate_without_fallback(monkeypatch, failure_site):
    client = _FakeClient([*[_round([_search_call(i)]) for i in range(1, 6)],
        _answer(""), _answer("Fallback must not run")])
    kernel = _kernel(client, lambda *_a, **_k: EMPTY_RESULT)
    created = []

    def create(model):
        created.append(model.model)
        return client

    async def fail(**_payload):
        if failure_site == "model_request_fail":
            raise ValueError("discard settlement failed")
        return "failed"

    async def persist(*_a, **_k):
        raise ValueError("discard persistence failed")

    monkeypatch.setattr(kernel._deps, "create_client", create)
    if failure_site == "persist":
        monkeypatch.setattr(kernel, "_persist_before_exit", persist)
    request = _request()
    request.fallback_model = SimpleNamespace(provider="openai", model="fallback")
    request.model_request_prepare = lambda **p: f"provider-{p['round_index']}"
    request.model_request_fail = fail
    with pytest.raises(ValueError, match="discard (settlement|persistence) failed"):
        await kernel.handle(request)
    assert len(client.calls) == 6 and created == ["local"]


@pytest.mark.asyncio
async def test_summary_ptl_retry_keeps_tools_withheld_and_reapplies_its_instruction(monkeypatch):
    import app.kernel.engine as support
    from app.services.llm_client import LLMError

    class Client(_FakeClient):
        async def stream(self, **kwargs):
            if len(self.calls) == 5:
                self.calls.append(kwargs)
                raise LLMError("maximum context length exceeded", delivery_state="rejected", http_status=400)
            return await super().stream(**kwargs)

    compressed, prepared = [], []

    async def compress(_compressor, messages, **kwargs):
        if kwargs["instructions"] == "prompt_too_long_full_compress_first":
            compressed.append(True)
            return [{"role": "user", "content": "Prior searches returned no material."}]
        return messages

    monkeypatch.setattr(support, "_compress_messages_with_lifecycle_hooks", compress)
    client = Client([*[_round([_search_call(i)]) for i in range(1, 6)], _answer(EMPTY_SEARCH_FINAL_MESSAGE)])
    request = _request()
    request.model_request_prepare = lambda **p: prepared.append(p) or f"provider-{len(prepared)}"
    result = await _kernel(client, lambda *_a, **_k: EMPTY_RESULT).handle(request)
    assert compressed == [True] and len(client.calls) == 7
    assert result.terminal_reason is TerminalReason.TURN_STOP
    for call in client.calls[5:]:
        assert call["tools"] is None
        assert "Any tool call you return will not run." in _message_texts(call)
    for snapshot in prepared[5:]:
        assert snapshot["tools"] is None
        assert "Any tool call you return will not run." in json.dumps(snapshot["wire_request"]["messages"])
