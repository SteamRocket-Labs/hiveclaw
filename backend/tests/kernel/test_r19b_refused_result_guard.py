"""A fixed non-retryable result refusal stops paging at the existing threshold."""

import json

import pytest

from app.kernel.contracts import TerminalReason
from app.kernel.loop_guard import LoopGuard
from tests.kernel.test_r18a_empty_search_guard import _FakeClient, _answer, _kernel, _request, _round

MESSAGE = "同事的结果这次没法安全读取，我没能完成整理。你可以让我重新安排这项工作。"
REF = "runtime-result://11111111-1111-1111-1111-111111111111/" + "a" * 64


def _page(status="source_access_denied", *, retryable=False, result_ref=REF, reason="content_source_changed"):
    return {"schema": "hive.runtime_result_page.v1", "status": status, "result_ref": result_ref,
        "reason_code": reason, "retryable": retryable, "recovery_action": "reconcile_result_sources"}


def _call(index, *, result_ref=REF):
    return {"id": f"read-{index}", "function": {"name": "read_runtime_result", "arguments": json.dumps({
        "result_ref": result_ref, "offset": index * 256, "limit": 6000 + index})}}


@pytest.mark.parametrize("status", ["source_access_denied", "source_unavailable", "authority_denied", "integrity_error"])
async def test_non_retryable_result_refusal_ends_real_kernel_at_identical_read_threshold(status):
    executed = []

    async def execute(tool_name, arguments, *_args, **_kwargs):
        executed.append(arguments)
        return json.dumps(_page(status))

    threshold = LoopGuard().identical_tool_threshold
    client = _FakeClient([*[_round([_call(index)]) for index in range(threshold + 2)],
        _answer("The model kept paging the refused result.")])
    result = await _kernel(client, execute, tools=("read_runtime_result",)).handle(_request())
    assert len(executed) == len(client.calls) == threshold
    assert len({(args["offset"], args["limit"]) for args in executed}) == threshold
    assert result.terminal_reason is TerminalReason.LOOP_GUARD
    assert result.failure_code == "identical_refused_result_read"
    assert result.failure_retryable is False
    assert result.content == MESSAGE


@pytest.mark.parametrize("status", ["not_found", "source_unavailable"])
async def test_retryable_result_pages_keep_the_existing_kernel_behavior(status):
    executed = []

    async def execute(tool_name, arguments, *_args, **_kwargs):
        executed.append(arguments)
        return json.dumps(_page(status, retryable=True))

    count = LoopGuard().identical_tool_threshold + 2
    client = _FakeClient([*[_round([_call(index)]) for index in range(count)], _answer("Still waiting for the result.")])
    result = await _kernel(client, execute, tools=("read_runtime_result",)).handle(_request())
    assert len(executed) == count
    assert result.content == "Still waiting for the result."
    assert result.terminal_reason is not TerminalReason.LOOP_GUARD


def test_refusal_identity_and_page_must_match_and_other_tools_do_not_reset_the_count():
    guard = LoopGuard()
    other_ref = REF.replace("11111111-", "22222222-", 1)
    for index in range(guard.identical_tool_threshold - 1):
        for result_ref in (REF, other_ref):
            assert guard.observe_tool_result("read_runtime_result", {"result_ref": result_ref, "offset": index},
                json.dumps(_page(result_ref=result_ref)), side_effect_free=True) is None
        assert guard.observe_tool_result("read_file", {"path": "workspace/notes.md"}, "unchanged",
            side_effect_free=True) is None
    # A different refusal and a page for the wrong reference are not this proof.
    assert guard.observe_tool_result("read_runtime_result", {"result_ref": REF},
        json.dumps(_page(reason="content_source_access_denied")), side_effect_free=True) is None
    assert guard.observe_tool_result("read_runtime_result", {"result_ref": REF},
        json.dumps(_page(result_ref=other_ref)), side_effect_free=True) is None
    decision = guard.observe_tool_result("read_runtime_result", {"result_ref": REF},
        json.dumps(_page()), side_effect_free=True)
    assert decision is not None and decision.reason == "identical_refused_result_read"


def test_refusal_requires_the_native_schema_explicit_non_retryability_and_read_only_tool():
    guard = LoopGuard()
    for page in ({**_page(), "schema": "other"}, {**_page(), "retryable": None}):
        for _ in range(guard.identical_tool_threshold + 1):
            assert guard.observe_tool_result("read_runtime_result", {"result_ref": REF}, json.dumps(page),
                side_effect_free=True) is None
    for _ in range(guard.identical_tool_threshold + 1):
        assert guard.observe_tool_result("read_runtime_result", {"result_ref": REF}, json.dumps(_page()),
            side_effect_free=False) is None
