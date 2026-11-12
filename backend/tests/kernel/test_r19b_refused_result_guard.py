"""A fixed non-retryable result refusal stops paging at the existing threshold."""

import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.kernel.contracts import TerminalReason
from app.kernel.loop_guard import LoopGuard
from tests.kernel.test_r18a_empty_search_guard import _FakeClient, _answer, _kernel, _request, _round

# Approval, remote, subagent and creation results also reach this code; the
# sentence must name neither a colleague nor a summarization task.
MESSAGE = "这份结果这次没法安全读取，我没能继续。你可以让我重新安排这项工作。"
REF = "runtime-result://11111111-1111-1111-1111-111111111111/" + "a" * 64


def _page(status="source_access_denied", *, retryable=False, result_ref=REF, reason="content_source_changed"):
    return {"schema": "hive.runtime_result_page.v1", "status": status, "result_ref": result_ref,
        "reason_code": reason, "retryable": retryable, "recovery_action": "reconcile_result_sources"}


def _call(index, *, result_ref=REF):
    return {"id": f"read-{index}", "function": {"name": "read_runtime_result", "arguments": json.dumps({
        "result_ref": result_ref, "offset": index * 256, "limit": 6000 + index})}}


# Native authority_denied pages have no result_ref and cannot be this proof.
@pytest.mark.parametrize("status", ["source_access_denied", "source_unavailable", "integrity_error"])
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
            # Each observation here models a new round. The real kernel observes
            # every call before any result, so its call count also identifies batches.
            guard.observe_tool_call("read_runtime_result", {"result_ref": result_ref, "offset": index})
            assert guard.observe_tool_result("read_runtime_result", {"result_ref": result_ref, "offset": index},
                json.dumps(_page(result_ref=result_ref)), side_effect_free=True) is None
        assert guard.observe_tool_result("read_file", {"path": "workspace/notes.md"}, "unchanged",
            side_effect_free=True) is None
    # A different refusal and a page for the wrong reference are not this proof.
    assert guard.observe_tool_result("read_runtime_result", {"result_ref": REF},
        json.dumps(_page(reason="content_source_access_denied")), side_effect_free=True) is None
    assert guard.observe_tool_result("read_runtime_result", {"result_ref": REF},
        json.dumps(_page(result_ref=other_ref)), side_effect_free=True) is None
    guard.observe_tool_call("read_runtime_result", {"result_ref": REF, "offset": 9999})
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


@pytest.mark.parametrize("status_code", [503, 409, 403])
async def test_native_source_refusal_retryability_reaches_the_real_kernel(monkeypatch, tmp_path, status_code):
    from app.services import runtime_result_sources
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.tools.handlers import context_resources
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    payload = b"scripted result"
    sha256 = hashlib.sha256(payload).hexdigest()
    result_ref = REF.rsplit("/", 1)[0] + "/" + sha256
    result_object = SimpleNamespace(source_kind="subagent", payload_bytes=payload, size_bytes=len(payload), sha256=sha256)
    db = SimpleNamespace()

    async def execute_query(_statement):
        return SimpleNamespace(scalar_one_or_none=lambda: result_object)

    db.execute = execute_query

    @asynccontextmanager
    async def source_db(*_args, **_kwargs):
        yield db

    async def publication_bound(*_args, **_kwargs):
        return True

    async def unavailable(*_args, **_kwargs):
        raise ContentInputReceiptError("content_source_unavailable", "The current context source could not be read. Retry later.", status_code)

    monkeypatch.setattr(context_resources, "tenant_scoped_session", source_db)
    monkeypatch.setattr(runtime_result_sources, "runtime_result_publication_bound", publication_bound)
    monkeypatch.setattr(runtime_result_sources, "validate_runtime_result_sources", unavailable)
    context = ToolExecutionContext(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()),
        session_id=str(uuid4()), workspace=tmp_path)
    pages = []

    async def execute(_name, args, *_args, **_kwargs):
        page = await context_resources.read_runtime_result(ToolExecutionRequest(
            tool_name="read_runtime_result", arguments=args, context=context))
        pages.append(json.loads(page))
        return page

    threshold = LoopGuard().identical_tool_threshold
    client = _FakeClient([*[_round([_call(i, result_ref=result_ref)]) for i in range(threshold)],
        _answer("The transient read can recover.")])
    result = await _kernel(client, execute, tools=("read_runtime_result",)).handle(_request())
    assert len(pages) == threshold
    if status_code == 503:
        assert result.terminal_reason is not TerminalReason.LOOP_GUARD
        assert result.content == "The transient read can recover."
        assert all(page["retryable"] is True for page in pages)
    else:
        assert result.terminal_reason is TerminalReason.LOOP_GUARD
        assert result.failure_code == "identical_refused_result_read"
        assert all(page["retryable"] is False for page in pages)


@pytest.mark.parametrize("recovery", [
    {"schema": "hive.runtime_result_page.v1", "status": "ok", "result_ref": REF},
    _page("not_found", retryable=True),
    _page("source_unavailable", retryable=True),
])
async def test_non_refused_page_resets_that_result_count_in_real_kernel(recovery):
    pages = iter([*[_page() for _ in range(4)], recovery, _page()])

    async def execute(*_args, **_kwargs):
        return json.dumps(next(pages))

    client = _FakeClient([*[_round([_call(i)]) for i in range(6)], _answer("The read made progress.")])
    result = await _kernel(client, execute, tools=("read_runtime_result",)).handle(_request())
    assert result.terminal_reason is not TerminalReason.LOOP_GUARD
    assert result.content == "The read made progress."
    assert len(client.calls) == 7


@pytest.mark.parametrize("later_rounds", [0, 4])
async def test_parallel_refusals_count_once_per_model_round(later_rounds):
    executed = []

    async def execute(_name, args, *_args, **_kwargs):
        executed.append(args)
        return json.dumps(_page())

    client = _FakeClient([_round([_call(i) for i in range(5)]),
        *[_round([_call(5 + i)]) for i in range(later_rounds)], _answer("The first batch has been observed.")])
    result = await _kernel(client, execute, tools=("read_runtime_result",)).handle(_request())
    assert len(executed) == 5 + later_rounds
    if later_rounds:
        assert len(client.calls) == LoopGuard().identical_tool_threshold
        assert result.failure_code == "identical_refused_result_read"
    else:
        assert len(client.calls) == 2
        assert result.terminal_reason is not TerminalReason.LOOP_GUARD
        assert result.content == "The first batch has been observed."


def test_refusal_trace_reports_the_actual_abort_threshold():
    guard = LoopGuard()
    decision = None
    for i in range(guard.identical_tool_threshold):
        args = {"result_ref": REF, "offset": i}
        guard.observe_tool_call("read_runtime_result", args)
        decision = guard.observe_tool_result("read_runtime_result", args, json.dumps(_page()), side_effect_free=True)
    assert decision is not None and decision.reason == "identical_refused_result_read"
    assert decision.trace_event["runtime_outcome"]["details"]["abort_threshold"] == guard.identical_tool_threshold
