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


def _kernel(client: _FakeClient, execute_tool, *, max_tool_rounds: int = 50):
    from app.kernel import AgentKernel, KernelDependencies, RuntimeConfig

    return AgentKernel(KernelDependencies(
        resolve_runtime_config=lambda *_a, **_k: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=max_tool_rounds),
        resolve_current_user_name=lambda *_a, **_k: "Member",
        build_system_prompt=lambda *_a, **_k: "PROMPT",
        resolve_memory_context=lambda *_a, **_k: "",
        get_tools=lambda *_a, **_k: [
            {"type": "function", "function": {"name": "search_company_kb", "description": "",
                                              "parameters": {"type": "object"}}}
        ],
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
    # R18a round 2: the stop ends as a loop-guard failure so delegated and
    # business lanes settle too, instead of a TURN_STOP without a receipt.
    assert result.terminal_reason is TerminalReason.LOOP_GUARD
    assert result.failure_code == "repeated_empty_search"
    assert result.failure_retryable is False
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


def test_guard_unit_material_found_disables_the_stop_for_the_turn():
    """A typed non-empty search result means material exists; only warnings
    may fire for the rest of the turn."""
    guard = LoopGuard()
    guard.observe_tool_result("search_company_kb", {"query": "hit"}, OK_RESULT,
        machine_outcome="succeeded", side_effect_free=True)
    severities = []
    for index in range(9):
        decision = guard.observe_tool_result("search_company_kb", {"query": index}, EMPTY_RESULT,
            machine_outcome="succeeded", side_effect_free=True)
        severities.append(decision.severity if decision else None)
        guard.observe_tool_call("search_company_kb", {"query": index})  # rounds keep advancing
    assert severities == [None, None, "warn"] + [None] * 6


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
async def test_found_material_then_empties_never_stops_with_the_sentence():
    """R18a round 2 ruling 3: material was found, so the nothing-found stop
    sentence would be false; the model answers with what it found."""

    executed: list[dict] = []

    async def execute_tool(tool_name, args, *_a, **_k):
        executed.append({"tool": tool_name, "args": args})
        return OK_RESULT if len(executed) <= 2 else EMPTY_RESULT

    responses = [
        SimpleNamespace(content="", tool_calls=_parallel_search_calls(1, 2),
                        reasoning_content=None, usage={"total_tokens": 5}),
        SimpleNamespace(content="", tool_calls=_parallel_search_calls(3, 5),
                        reasoning_content=None, usage={"total_tokens": 5}),
        _answer("找到了一些资料，没有「白鹭计划」专档。"),
    ]
    client = _FakeClient(responses)
    kernel = _kernel(client, execute_tool)
    result = await kernel.handle(_request())

    assert len(executed) == 7
    assert result.content == "找到了一些资料，没有「白鹭计划」专档。"
    assert "多次搜索都没有找到相关资料。" not in result.content


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
