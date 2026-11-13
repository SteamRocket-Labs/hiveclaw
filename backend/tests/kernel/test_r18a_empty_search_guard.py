"""R18a Part 1: repeated empty search results end with a plain member answer.

E2E run 15: the member asked for 「白鹭计划」 material, the model issued 40
search_company_kb calls that all returned {"status": "empty"}, and the member
never got an answer. The guard now warns the model after a few empty results
and ends the turn with the plain stop sentence if the searching continues.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.kernel.loop_guard import LoopGuard
from app.services.runtime_display_status import EMPTY_SEARCH_FINAL_MESSAGE

EMPTY_RESULT = json.dumps({"status": "empty", "results": [], "note": "no match"}, ensure_ascii=False)
OK_RESULT = json.dumps({"status": "ok", "results": [{"id": "hit-1"}]}, ensure_ascii=False)


class _FakeClient:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("No fake response prepared")
        return self._responses.pop(0)

    async def close(self) -> None:
        return None


def _search_call(index: int) -> dict:
    # Every call rewords the query: the rule must count results, not arguments.
    return {
        "id": f"call_{index}",
        "function": {"name": "search_company_kb", "arguments": json.dumps({"query": f"白鹭计划 资料 第{index}种说法"})},
    }


def _answer(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=text, tool_calls=[], reasoning_content=None, usage={"total_tokens": 5})


def _search_tool_def(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": "",
                                             "parameters": {"type": "object"}}}


def _kernel(client: _FakeClient, execute_tool, *, max_tool_rounds: int = 50, tools=("search_company_kb",)):
    from app.kernel import AgentKernel, KernelDependencies, RuntimeConfig

    return AgentKernel(KernelDependencies(
        resolve_runtime_config=lambda *_a, **_k: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=max_tool_rounds),
        resolve_current_user_name=lambda *_a, **_k: "Member",
        build_system_prompt=lambda *_a, **_k: "PROMPT",
        resolve_memory_context=lambda *_a, **_k: "",
        get_tools=lambda *_a, **_k: [_search_tool_def(name) for name in tools],
        maybe_compress_messages=lambda messages, **_k: messages,
        create_client=lambda _m: client,
        execute_tool=execute_tool,
        persist_memory=lambda **_k: None,
        record_token_usage=lambda *_a, **_k: None,
        get_max_tokens=lambda *_a, **_k: 2048,
        extract_usage_tokens=lambda usage: usage.get("total_tokens"),
        estimate_tokens_from_chars=lambda n: n // 4,
    ))


def _request() -> "object":
    from app.kernel import InvocationRequest

    return InvocationRequest(
        model=SimpleNamespace(provider="openai", model="local", api_key="local", base_url=None),
        messages=[{"role": "user", "content": "查一下公司知识库里有没有关于「白鹭计划」的资料"}],
        agent_name="Agent", role_description="", agent_id=uuid4(), user_id=uuid4(),
    )


def _message_texts(call: dict) -> str:
    parts = []
    for message in call.get("messages") or []:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            parts.append(content)
        elif content is not None:
            parts.append(str(content))
    return "\n".join(parts)


@pytest.mark.asyncio
async def test_rewording_forever_model_ends_with_the_plain_sentence():
    """(a) The turn ends at the abort threshold with the member sentence."""

    executed: list[dict] = []

    async def execute_tool(tool_name, args, *_a, **_k):
        executed.append({"tool": tool_name, "args": args})
        return EMPTY_RESULT

    class RewordingClient(_FakeClient):
        async def stream(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                content="",
                tool_calls=[_search_call(len(executed) + 1)],
                reasoning_content=None,
                usage={"total_tokens": 5},
            )

    from app.kernel.contracts import TerminalReason

    client = RewordingClient([])
    kernel = _kernel(client, execute_tool)
    result = await kernel.handle(_request())

    assert [call["tool"] for call in executed] == ["search_company_kb"] * 5
    assert result.content == EMPTY_SEARCH_FINAL_MESSAGE
    # R20b round 3: the stubborn base fake gets one summary call; its discarded
    # search cannot execute, so the original failure contract settles the turn.
    assert result.terminal_reason is TerminalReason.LOOP_GUARD
    assert result.failure_code == "repeated_empty_search"
    assert result.failure_retryable is False
    # R20b round 3: five executed searches plus exactly one bounded summary call.
    assert len(client.calls) == 6 and client.calls[-1]["tools"] is None
    # The warning fired after the third empty result: the 4th model round saw
    # it (and it stays in history for the 5th), never before that.
    assert "[Loop Guard Warning]" not in _message_texts(client.calls[2])
    assert "[Loop Guard Warning]" in _message_texts(client.calls[3])


@pytest.mark.asyncio
async def test_model_that_answers_after_one_empty_search_gets_no_warning():
    """(b) One empty search and an honest answer: no guard interference."""

    executed: list[dict] = []

    async def execute_tool(tool_name, args, *_a, **_k):
        executed.append({"tool": tool_name, "args": args})
        return EMPTY_RESULT

    client = _FakeClient([
        SimpleNamespace(content="", tool_calls=[_search_call(1)], reasoning_content=None, usage={"total_tokens": 5}),
        _answer("公司知识库里没有找到关于「白鹭计划」的资料。"),
    ])
    kernel = _kernel(client, execute_tool)
    result = await kernel.handle(_request())

    assert len(executed) == 1
    assert result.content == "公司知识库里没有找到关于「白鹭计划」的资料。"
    assert not any("[Loop Guard Warning]" in _message_texts(call) for call in client.calls)


@pytest.mark.asyncio
async def test_turn_whose_searches_return_results_is_unaffected():
    """(c) Fruitful searching never triggers the empty-result rule."""

    executed: list[dict] = []

    async def execute_tool(tool_name, args, *_a, **_k):
        executed.append({"tool": tool_name, "args": args})
        return OK_RESULT

    responses = [
        SimpleNamespace(content="", tool_calls=[_search_call(index)], reasoning_content=None, usage={"total_tokens": 5})
        for index in range(1, 7)
    ] + [_answer("找到了，以下是「白鹭计划」的相关资料。")]
    client = _FakeClient(responses)
    kernel = _kernel(client, execute_tool)
    result = await kernel.handle(_request())

    assert len(executed) == 6
    assert result.content == "找到了，以下是「白鹭计划」的相关资料。"
    assert not any("[Loop Guard Warning]" in _message_texts(call) for call in client.calls)


def test_guard_unit_warns_once_then_stops_with_the_plain_sentence():
    guard = LoopGuard()
    for query in ("a", "b"):
        assert guard.observe_tool_result("search_company_kb", {"query": query}, EMPTY_RESULT,
            machine_outcome="succeeded", side_effect_free=True) is None
    warn = guard.observe_tool_result("search_company_kb", {"query": "c"}, EMPTY_RESULT,
        machine_outcome="succeeded", side_effect_free=True)
    assert warn is not None and warn.severity == "warn"
    assert "tell the member" in warn.message
    assert "with different arguments" not in warn.message  # identical calls also warn truthfully
    assert ".." not in warn.message  # R18a round 3 nit e: the wrapper adds the period
    # A later model round (its tool calls) proves the warning was delivered.
    guard.observe_tool_call("search_company_kb", {"query": "d"})
    assert guard.observe_tool_result("search_company_kb", {"query": "d"}, EMPTY_RESULT,
        machine_outcome="succeeded", side_effect_free=True) is None
    stop = guard.observe_tool_result("search_company_kb", {"query": "e"}, EMPTY_RESULT,
        machine_outcome="succeeded", side_effect_free=True)
    assert stop is not None and stop.severity == "abort"
    assert stop.reason == "repeated_empty_search"
    assert stop.message == EMPTY_SEARCH_FINAL_MESSAGE
    assert stop.trace_event["empty_search_count"] == 5
    assert guard.failed_tool_calls == 0


def test_guard_unit_same_round_empties_after_the_warning_only_warn():
    """Five parallel empties in one round never stop: the batch resolves in
    the same round the warning fired, so the model has not seen it yet."""
    guard = LoopGuard()
    decisions = [
        guard.observe_tool_result("search_company_kb", {"query": index}, EMPTY_RESULT,
            machine_outcome="succeeded", side_effect_free=True)
        for index in range(5)
    ]
    assert [decision.severity if decision else None for decision in decisions] == [
        None, None, "warn", None, None,
    ]


def test_guard_unit_material_found_keeps_the_empty_streak_bound():
    """R21a reverses R18a r2: earlier material changes copy, not the bound."""
    guard = LoopGuard()
    guard.observe_tool_result("search_company_kb", {"query": "hit"}, OK_RESULT,
        machine_outcome="succeeded", side_effect_free=True)
    severities = []
    for index in range(5):
        decision = guard.observe_tool_result("search_company_kb", {"query": index}, EMPTY_RESULT,
            machine_outcome="succeeded", side_effect_free=True)
        severities.append(decision.severity if decision else None)
        guard.observe_tool_call("search_company_kb", {"query": index})  # rounds keep advancing
    # R21a: the warned later round must abort even though an earlier search hit.
    assert severities == [None, None, "warn", None, "abort"]


def test_guard_unit_non_empty_result_resets_the_consecutive_count():
    """Two empties, one fruitful search, then empties again: the new streak
    starts from zero, so no warning fires on its first two empties."""
    guard = LoopGuard()
    for query in ("a", "b"):
        guard.observe_tool_result("search_company_kb", {"query": query}, EMPTY_RESULT,
            machine_outcome="succeeded", side_effect_free=True)
        guard.observe_tool_call("search_company_kb", {"query": query})
    guard.observe_tool_result("search_company_kb", {"query": "hit"}, OK_RESULT,
        machine_outcome="succeeded", side_effect_free=True)
    for index in (1, 2):
        guard.observe_tool_call("search_company_kb", {"query": index})
        assert guard.observe_tool_result("search_company_kb", {"query": index}, EMPTY_RESULT,
            machine_outcome="succeeded", side_effect_free=True) is None


@pytest.mark.parametrize("tool_name", ["read_file", "check_async_task", "list_files"])
def test_guard_unit_ignores_non_search_tools_and_non_empty_results(tool_name):
    guard = LoopGuard()
    for _ in range(10):
        assert guard.observe_tool_result(tool_name, {"q": "x"}, EMPTY_RESULT,
            machine_outcome="succeeded", side_effect_free=True) is None
    for _ in range(10):
        assert guard.observe_tool_result("search_company_kb", {"q": "x"}, OK_RESULT,
            machine_outcome="succeeded", side_effect_free=True) is None
    for _ in range(10):
        assert guard.observe_tool_result("search_company_kb", {"q": "x"}, "not json",
            machine_outcome="succeeded", side_effect_free=True) is None
    # A search tool the runtime did not classify side-effect-free is not ours to stop.
    for _ in range(10):
        assert guard.observe_tool_result("search_company_kb", {"q": "x"}, EMPTY_RESULT,
            machine_outcome="succeeded", side_effect_free=False) is None


def _parallel_search_calls(start_index: int, count: int) -> list[dict]:
    return [
        {"id": f"call_{start_index + offset}", "function": {
            "name": "search_company_kb", "arguments": json.dumps({"query": f"白鹭计划 第{start_index + offset}种说法"})}}
        for offset in range(count)
    ]


@pytest.mark.asyncio
async def test_five_parallel_empties_in_one_round_only_warn():
    """R18a round 2 ruling 3: one round of five parallel empty searches warns
    and the model is called again; the stop needs a later warned round."""

    executed: list[dict] = []

    async def execute_tool(tool_name, args, *_a, **_k):
        executed.append({"tool": tool_name, "args": args})
        return EMPTY_RESULT

    client = _FakeClient([
        SimpleNamespace(content="", tool_calls=_parallel_search_calls(1, 5),
                        reasoning_content=None, usage={"total_tokens": 5}),
        _answer("知识库里没有找到相关资料。"),
    ])
    kernel = _kernel(client, execute_tool)
    result = await kernel.handle(_request())

    assert len(executed) == 5  # the whole parallel batch ran
    assert len(client.calls) == 2  # the model was called again after the warning
    assert "[Loop Guard Warning]" in _message_texts(client.calls[1])
    assert result.content == "知识库里没有找到相关资料。"


@pytest.mark.asyncio
async def test_found_material_then_empties_gets_a_summary_with_the_material():
    """R21a: R20b's model-written summary replaces the false nothing-found stop."""

    executed: list[dict] = []

    async def execute_tool(tool_name, args, *_a, **_k):
        executed.append({"tool": tool_name, "args": args})
        return OK_RESULT if len(executed) <= 2 else EMPTY_RESULT

    # One empty per round reaches the abort threshold in a warned later round.
    responses = [
        SimpleNamespace(content="", tool_calls=_parallel_search_calls(1, 2),
                        reasoning_content=None, usage={"total_tokens": 5}),
        *[SimpleNamespace(content="", tool_calls=_parallel_search_calls(3 + offset, 1),
                          reasoning_content=None, usage={"total_tokens": 5})
          for offset in range(5)],
        _answer("找到了一些资料，没有「白鹭计划」专档。"),
    ]
    client = _FakeClient(responses)
    kernel = _kernel(client, execute_tool)
    result = await kernel.handle(_request())

    assert len(executed) == 7
    assert result.content == "找到了一些资料，没有「白鹭计划」专档。"
    assert "多次搜索都没有找到相关资料。" not in result.content
    assert client.calls[-1]["tools"] is None


@pytest.mark.asyncio
async def test_run15_shape_with_earlier_tool_search_still_stops():
    """E2E run 15's shape: one tool_search, then 40 empty search_company_kb
    rounds — an unrelated non-search tool does not disarm the stop."""

    executed: list[dict] = []

    async def execute_tool(tool_name, args, *_a, **_k):
        executed.append({"tool": tool_name, "args": args})
        if tool_name == "tool_search":
            return json.dumps({"results": [{"name": "search_company_kb"}]}, ensure_ascii=False)
        return EMPTY_RESULT

    class RewordingClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__([])
            self._first = True

        async def stream(self, **kwargs):
            self.calls.append(kwargs)
            if self._first:
                self._first = False
                return SimpleNamespace(content="", tool_calls=[{
                    "id": "call_tool_search", "function": {
                        "name": "tool_search", "arguments": json.dumps({"query": "knowledge"})}}],
                    reasoning_content=None, usage={"total_tokens": 5})
            return SimpleNamespace(content="", tool_calls=[_search_call(len(executed))],
                reasoning_content=None, usage={"total_tokens": 5})

    client = RewordingClient()
    kernel = _kernel(client, execute_tool)
    result = await kernel.handle(_request())

    assert executed[0]["tool"] == "tool_search"
    assert [call["tool"] for call in executed[1:]] == ["search_company_kb"] * 5
    assert result.content == EMPTY_SEARCH_FINAL_MESSAGE
    from app.kernel.contracts import TerminalReason

    # R20b round 3: discovery plus five searches execute; only one extra model call is allowed.
    assert len(client.calls) == 7
    assert result.terminal_reason is TerminalReason.LOOP_GUARD
    assert result.failure_code == "repeated_empty_search"


def _call(name: str, index: int) -> dict:
    return {"id": f"{name}-{index}", "type": "function",
            "function": {"name": name, "arguments": json.dumps({"query": f"白鹭计划 第{index}种说法"})}}


def _round(calls: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(content="", tool_calls=calls, reasoning_content=None, usage={"total_tokens": 5})


@pytest.mark.asyncio
async def test_prose_hit_then_empties_gets_a_summary_with_the_material():
    """R21a: prose hits still count as material, now in the bounded summary."""

    executed: list[dict] = []

    async def execute_tool(tool_name, args, *_a, **_k):
        executed.append({"tool": tool_name})
        if tool_name == "search_memory":
            return "找到一条记忆：白鹭计划是 3 月启动的内部项目。"
        return EMPTY_RESULT

    responses = [
        _round([_call("search_memory", 1)]),
        *[_round([_call("search_company_kb", index)]) for index in range(2, 7)],
        _answer("记忆里有一条「白鹭计划」的记录，公司知识库里没有更多资料。"),
    ]
    client = _FakeClient(responses)
    kernel = _kernel(client, execute_tool, tools=("search_company_kb", "search_memory"))
    result = await kernel.handle(_request())

    assert [call["tool"] for call in executed] == ["search_memory"] + ["search_company_kb"] * 5
    assert result.content == "记忆里有一条「白鹭计划」的记录，公司知识库里没有更多资料。"
    assert client.calls[-1]["tools"] is None


MATERIAL_FALLBACK = "已停止继续搜索，但没能整理出答复。你可以让我根据目前的结果直接作答。"


@pytest.mark.asyncio
@pytest.mark.parametrize("material", [
    json.dumps({"status": "partial", "results": [{"text": "本周完成接口联调", "verified": False}]},
               ensure_ascii=False),
    "本周完成接口联调；平常周报格式尚未确认。",
], ids=["typed_partial", "prose_memory"])
@pytest.mark.parametrize("first_tool", ["search_personal_kb", "search_company_kb"])
@pytest.mark.parametrize("summary_kind", ["answer", "tool", "empty"])
async def test_run18_material_then_alternating_empty_searches_get_one_final_reply(
    material, first_tool, summary_kind,
):
    """Real handle: either KB tool can trip its own five-empty warned streak."""
    from app.kernel.contracts import TerminalReason
    from app.kernel.engine import _llm_messages_to_dicts

    second_tool = "search_company_kb" if first_tool == "search_personal_kb" else "search_personal_kb"
    executed, prepared, failures, persisted = [], [], [], []
    answer = "本周完成接口联调。没有找到你平常的周报格式，相关来源也尚未核实。"

    async def execute(name, args, *_a, **_k):
        executed.append((name, args))
        return material if name == "search_memory" else EMPTY_RESULT

    summary = {"answer": _answer(answer), "tool": _round([_call(first_tool, 100)]), "empty": _answer("")}
    script = [_round([_call("search_memory", 0)]),
        *[_round([_call(first_tool if i % 2 else second_tool, i)]) for i in range(1, 10)],
        summary[summary_kind], _answer("This extra round must not run")]
    client = _FakeClient(script)
    kernel = _kernel(client, execute, tools=("search_memory", first_tool, second_tool))

    async def persist(_request, _config, content, messages):
        persisted.append((content, list(messages)))

    kernel._persist_before_exit = persist
    request = _request()
    request.model_request_prepare = lambda **p: prepared.append(p) or f"provider-{len(prepared)}"
    request.model_request_fail = lambda **p: failures.append(p)
    result = await kernel.handle(request)

    assert [name for name, _args in executed] == ["search_memory"] + [
        first_tool if i % 2 else second_tool for i in range(1, 10)]
    assert len(client.calls) == 11  # material, nine empties, one summary
    assert client.calls[-1]["tools"] is None
    assert "[Loop Guard Warning]" not in _message_texts(client.calls[5])
    assert "answer with the material already found" in _message_texts(client.calls[6])
    instruction = client.calls[-1]["messages"][-1]
    assert "Stop searching" in instruction.content
    assert "material already found in this turn" in instruction.content
    assert "could not be found or confirmed" in instruction.content
    assert "action already committed" in instruction.content
    assert "Do not claim actions that were only proposed, denied, failed, or unknown." in instruction.content
    assert "This is the final reply for this turn. Any tool call you return will not run." in instruction.content
    assert "no matching material was found" not in instruction.content
    assert prepared[-1]["wire_request"]["messages"] == _llm_messages_to_dicts(client.calls[-1]["messages"])
    assert prepared[-1]["messages"] == client.calls[-1]["messages"]
    assert instruction.context_origin.unbound == ()
    facts = [message for message in client.calls[-1]["messages"]
        if "<loop_guard_terminal_evidence>" in str(message.content)]
    assert len(facts) == 1
    assert "The last searches returned no new material." in facts[0].content
    assert facts[0].context_origin.unbound == ()
    assert "The searches returned no matching material." not in facts[0].content
    assert "Any tool call you return will not run." not in facts[0].content
    history = (result.response_complete_payload["messages"] if summary_kind == "answer"
        else _llm_messages_to_dicts(persisted[-1][1]))
    assert sum(message.get("content") == facts[0].content for message in history) == 1
    assert all(message.get("content") != instruction.content for message in history)  # request-only, as in R20b r5
    if summary_kind == "answer":
        assert result.terminal_reason is TerminalReason.TURN_STOP
        assert result.content == answer and result.failure_code is None
        assert failures == []
    else:
        assert result.terminal_reason is TerminalReason.LOOP_GUARD
        assert result.failure_code == "repeated_empty_search" and result.failure_retryable is False
        assert result.content == MATERIAL_FALLBACK
        assert persisted[-1][0] == MATERIAL_FALLBACK
        assert failures == [{"round_index": 11, "provider_request_id": "provider-11",
            "continuation_index": 0, "error_class": "repeated_empty_search",
            "delivery_state": "response_received", "retry_safe": True}]


@pytest.mark.parametrize("sentence", [EMPTY_SEARCH_FINAL_MESSAGE, MATERIAL_FALLBACK])
def test_empty_search_frozen_sentences_survive_the_delegation_readers(sentence):
    from app.services.runtime_display_status import (
        delegation_failure_result_page, is_delegation_failure_result_page,
        is_runtime_failure_observation, runtime_failure_observation,
    )

    observation = runtime_failure_observation("failed", {"error_code": "repeated_empty_search"},
        result_summary=sentence)
    assert observation["reason"] == sentence and observation["retryable"] is False
    assert is_runtime_failure_observation(observation)
    page = delegation_failure_result_page(task_id=str(uuid4()), status="failed",
        metadata={"error_code": "repeated_empty_search"}, result_summary=sentence)
    assert page["result"] == sentence and is_delegation_failure_result_page(page)
    assert runtime_failure_observation("failed", {"error_code": "repeated_empty_search"},
        result_summary="Untrusted partial model output")["reason"] == EMPTY_SEARCH_FINAL_MESSAGE


@pytest.mark.asyncio
async def test_no_material_summary_instruction_and_fact_are_byte_identical():
    client = _FakeClient([*[_round([_search_call(i)]) for i in range(1, 6)], _answer(EMPTY_SEARCH_FINAL_MESSAGE)])
    kernel = _kernel(client, lambda *_a, **_k: EMPTY_RESULT)
    result = await kernel.handle(_request())
    assert client.calls[-1]["messages"][-1].content == (
        "Finish this turn with a normal assistant reply in the member's language, "
        "plainly saying that no matching material was found. "
        "Also report any action already committed in this same turn, grounded in successful tool results. "
        "Do not claim actions that were only proposed, denied, failed, or unknown. "
        "This is the final reply for this turn. Any tool call you return will not run.")
    facts = [message["content"] for message in result.response_complete_payload["messages"]
        if "<loop_guard_terminal_evidence>" in str(message.get("content"))]
    assert len(facts) == 1
    assert facts[0].split("</loop_guard_terminal_evidence>\n")[1] == "The searches returned no matching material."


@pytest.mark.asyncio
@pytest.mark.parametrize("hit_first", [False, True])
async def test_same_round_material_reaches_summary_even_after_an_earlier_hit(hit_first):
    """Controller ruling R21a r2 item 1: withdrawal removed, the summary round reads the material."""
    from app.kernel.contracts import TerminalReason

    executed = []

    async def execute(name, args, *_a, **_k):
        executed.append(name)
        return json.dumps({"status": "ok", "results": [{"id": f"hit-{args['query']}"}]}, ensure_ascii=False) if (
            name == "search_memory") else EMPTY_RESULT

    empties = [_call("search_company_kb", i) for i in range(4, 9)]
    hit = _call("search_memory", 9)
    client = _FakeClient([_round([_call("search_memory", 0)]),
        *[_round([_call("search_company_kb", i)]) for i in range(1, 4)],
        _round([hit, *empties] if hit_first else [*empties, hit]),
        _answer("已经找到新的资料。")])
    result = await _kernel(client, execute, tools=("search_memory", "search_company_kb")).handle(_request())
    assert len(executed) == 10 and len(client.calls) == 6
    assert client.calls[-1]["tools"] is None
    assert "hit-白鹭计划 第9种说法" in _message_texts(client.calls[-1])
    assert result.terminal_reason is TerminalReason.TURN_STOP and result.content == "已经找到新的资料。"


@pytest.mark.asyncio
async def test_typed_error_then_empties_still_stops():
    """R18a round 3 ruling 1: a typed invalid_request is not material — one
    malformed call must not re-arm the run-15 loop."""

    executed: list[dict] = []

    async def execute_tool(tool_name, args, *_a, **_k):
        executed.append({"tool": tool_name})
        if len(executed) == 1:
            return json.dumps({"status": "invalid_request", "warning": "query is required"}, ensure_ascii=False)
        return EMPTY_RESULT

    responses = [
        _round([_call("search_company_kb", 1)]),
        *[_round([_call("search_company_kb", index)]) for index in range(2, 20)],
    ]
    client = _FakeClient(responses)
    kernel = _kernel(client, execute_tool)
    result = await kernel.handle(_request())

    assert executed[0]["tool"] == "search_company_kb"
    assert len(executed) == 6  # one malformed call, then the abort threshold of empties
    assert result.content == EMPTY_SEARCH_FINAL_MESSAGE

    from app.kernel.contracts import TerminalReason

    # R20b round 3: the base stubborn script executes six calls and gets one discarded summary.
    assert len(client.calls) == 7
    assert result.terminal_reason is TerminalReason.LOOP_GUARD
    assert result.failure_code == "repeated_empty_search"


@pytest.mark.asyncio
async def test_warning_after_material_says_answer_with_what_was_found():
    """R18a round 3 ruling 2: once material exists, the warning must not tell
    the model to report nothing."""

    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard()
    guard.observe_tool_result("search_company_kb", {"query": "hit"}, OK_RESULT,
        machine_outcome="succeeded", side_effect_free=True)
    guard.observe_tool_call("search_company_kb", {"query": "a"})
    assert guard.observe_tool_result("search_company_kb", {"query": "a"}, EMPTY_RESULT,
        machine_outcome="succeeded", side_effect_free=True) is None
    guard.observe_tool_call("search_company_kb", {"query": "b"})
    assert guard.observe_tool_result("search_company_kb", {"query": "b"}, EMPTY_RESULT,
        machine_outcome="succeeded", side_effect_free=True) is None
    guard.observe_tool_call("search_company_kb", {"query": "c"})
    warn = guard.observe_tool_result("search_company_kb", {"query": "c"}, EMPTY_RESULT,
        machine_outcome="succeeded", side_effect_free=True)
    assert warn is not None and warn.severity == "warn"
    assert "answer with the material already found" in warn.message
    assert "nothing was found" not in warn.message


@pytest.mark.asyncio
async def test_fruitful_result_after_fifth_empty_in_same_round_reaches_summary():
    """Controller ruling R21a r2 item 1: withdrawal removed, the summary round reads the material."""

    executed: list[dict] = []

    async def execute_tool(tool_name, args, *_a, **_k):
        executed.append({"tool": tool_name, "args": args})
        if len(executed) <= 3:
            return EMPTY_RESULT  # three warned rounds, one search each
        return OK_RESULT if len(executed) == 9 else EMPTY_RESULT

    responses = [
        *[_round([_call("search_company_kb", index)]) for index in (1, 2, 3)],  # warn on the third
        _round([_call("search_company_kb", index) for index in (4, 5, 6, 7, 8)] + [_call("search_company_kb", 9)]),
        _answer("找到了「白鹭计划」的一条资料。"),
    ]
    client = _FakeClient(responses)
    kernel = _kernel(client, execute_tool)
    result = await kernel.handle(_request())

    assert len(executed) == 9  # the whole round ran, including the fruitful call
    assert len(client.calls) == 5  # the model now receives the bounded summary round
    assert client.calls[-1]["tools"] is None
    assert "hit-1" in _message_texts(client.calls[-1])
    assert result.content == "找到了「白鹭计划」的一条资料。"


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", ["没有找到与周报匹配的记忆", "周报包含进展、风险和下周计划。"], ids=["miss", "hit"])
@pytest.mark.parametrize("summary_kind", ["answer", "empty"])
async def test_every_round_prose_memory_and_empty_company_search_reaches_summary(memory, summary_kind):
    """Reviewer probe B: fresh prose in every round cannot withdraw the bound."""
    from app.kernel.contracts import TerminalReason

    executed = []
    answer = "目前的搜索结果不足以确认你的周报格式。"

    class Client(_FakeClient):
        async def stream(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["tools"] is None:
                return _answer(answer if summary_kind == "answer" else "")
            index = len(self.calls)
            return _round([_call("search_memory", index), _call("search_company_kb", index)])

    async def execute(name, *_a, **_k):
        executed.append(name)
        return memory if name == "search_memory" else EMPTY_RESULT

    client = Client([])
    result = await _kernel(client, execute, max_tool_rounds=12,
        tools=("search_memory", "search_company_kb")).handle(_request())
    assert len(client.calls) == 6
    assert len(executed) == 10
    assert executed.count("search_memory") == executed.count("search_company_kb") == 5
    assert client.calls[-1]["tools"] is None
    assert memory in _message_texts(client.calls[-1])
    assert "material already found in this turn" in _message_texts(client.calls[-1])
    if summary_kind == "answer":
        assert result.terminal_reason is TerminalReason.TURN_STOP and result.content == answer
    else:
        assert result.terminal_reason is TerminalReason.LOOP_GUARD
        assert result.failure_code == "repeated_empty_search" and not result.failure_retryable
        assert result.content == MATERIAL_FALLBACK


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [OK_RESULT, "周报格式：进展、风险、下周计划。"], ids=["typed_hit", "prose"])
@pytest.mark.parametrize("first_tool", ["search_personal_kb", "search_company_kb"])
async def test_run18_paired_parallel_empty_kb_searches_reach_summary(memory, first_tool):
    """N1: the production paired shape retains separate counts and one summary."""
    from app.kernel.contracts import TerminalReason

    second_tool = "search_company_kb" if first_tool == "search_personal_kb" else "search_personal_kb"
    executed = []

    async def execute(name, *_a, **_k):
        executed.append(name)
        return memory if name == "search_memory" else EMPTY_RESULT

    answer = "已根据现有结果整理周报，未确认的部分已注明。"
    client = _FakeClient([_round([_call("search_memory", 0)]),
        *[_round([_call(first_tool, i), _call(second_tool, i)]) for i in range(1, 6)], _answer(answer)])
    result = await _kernel(client, execute, tools=("search_memory", first_tool, second_tool)).handle(_request())
    assert len(client.calls) == 7
    assert len(executed) == 11 and executed[0] == "search_memory"
    assert executed.count(first_tool) == executed.count(second_tool) == 5
    assert client.calls[-1]["tools"] is None
    assert "[Loop Guard Warning]" not in _message_texts(client.calls[3])
    assert "[Loop Guard Warning]" in _message_texts(client.calls[4])
    assert result.terminal_reason is TerminalReason.TURN_STOP and result.content == answer


@pytest.mark.asyncio
async def test_other_tools_warning_does_not_authorize_this_tools_stop():
    """R18a round 3 ruling 4: the armed set is per tool — a personal-KB
    warning does not authorize a company-KB stop in the next round."""

    executed: list[dict] = []

    async def execute_tool(tool_name, args, *_a, **_k):
        executed.append({"tool": tool_name})
        return EMPTY_RESULT

    responses = [
        *[_round([_call("search_personal_kb", index)]) for index in (1, 2, 3)],  # warns on the third
        _round([_call("search_company_kb", index) for index in (4, 5, 6, 7, 8)]),  # five parallel empties
        _answer("个人知识库和公司知识库都没有相关资料。"),
    ]
    client = _FakeClient(responses)
    kernel = _kernel(client, execute_tool, tools=("search_company_kb", "search_personal_kb"))
    result = await kernel.handle(_request())

    assert len(client.calls) == 5  # warned for personal KB, company KB only warned
    assert result.content == "个人知识库和公司知识库都没有相关资料。"
