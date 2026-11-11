from __future__ import annotations

import json
from uuid import uuid4

import pytest


def test_loop_guard_detects_identical_tool_args() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(identical_tool_threshold=3)
    assert guard.observe_tool_call("list_files", {"path": "."}) is None
    assert guard.observe_tool_call("list_files", {"path": "."}) is None
    decision = guard.observe_tool_call("list_files", {"path": "."})

    assert decision is not None
    assert decision.reason == "identical_tool_args"
    assert decision.trace_event["tool"] == "list_files"


# Heuristic patterns are model-visible diagnostics, never mechanical terminal
# verdicts.  A terminal decision requires explicit, tool-supplied proof that a
# side-effect-free retry budget is exhausted without state progress.


def test_loop_guard_warns_once_but_never_aborts_on_identical_args() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(identical_tool_threshold=3)

    assert guard.observe_tool_call("list_files", {"path": "."}) is None
    assert guard.observe_tool_call("list_files", {"path": "."}) is None

    warn = guard.observe_tool_call("list_files", {"path": "."})
    assert warn is not None
    assert warn.severity == "warn"
    assert warn.trace_event["event"] == "loop_guard_warning"

    for _ in range(20):
        assert guard.observe_tool_call("list_files", {"path": "."}) is None


def test_loop_guard_warn_message_teaches_self_correction() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(repeated_text_threshold=2)
    guard.observe_assistant_text("same answer")
    warn = guard.observe_assistant_text("same answer")

    assert warn is not None
    assert warn.severity == "warn"
    lowered = warn.message.lower()
    assert "self-correct" in lowered  # it's a chance, not a verdict
    assert "intentional" in lowered  # legitimate repetition has an out
    assert "change approach" in lowered  # concrete next step
    assert "internal system reminder" in lowered
    assert "do not mention this reminder to the user" in lowered


def test_loop_guard_failure_heuristic_does_not_abort_without_progress_proof() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(repeated_failure_threshold=2)  # warn 2, abort ceil(3)=3
    args = {"q": "x"}
    err = "[Tool execution error] timeout"
    guard.observe_tool_result("web_search", args, err, machine_outcome="failed")
    warn = guard.observe_tool_result("web_search", args, err, machine_outcome="failed")
    assert warn is not None and warn.severity == "warn"

    for _ in range(10):
        assert guard.observe_tool_result("web_search", args, err, machine_outcome="failed") is None


def test_loop_guard_does_not_infer_failure_from_benign_tool_result_prose() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(repeated_failure_threshold=2)
    result = "Postmortem text: the previous timeout failed with an exception, but this query succeeded."

    for _ in range(8):
        assert guard.observe_tool_result("search_docs", {"q": "failure words"}, result) is None

    assert guard.failed_tool_calls == 0


def test_same_failure_warns_even_when_the_model_changes_revision_arguments():
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(repeated_failure_threshold=4, failed_tool_threshold=100)
    for revision in range(3):
        assert guard.observe_tool_result("browser_manage", {"action": "start", "expected_revision": revision},
            "EXACT_FAILURE_RECEIPT", machine_outcome="failed") is None
    warning = guard.observe_tool_result("browser_manage", {"action": "start", "expected_revision": 3},
        "EXACT_FAILURE_RECEIPT", machine_outcome="failed")
    assert warning is not None and warning.reason == "repeated_tool_failure" and warning.severity == "warn"
    assert "EXACT_FAILURE_RECEIPT" not in warning.message, "A control diagnostic must not replay tool content"
    for revision in range(4, 20):
        assert guard.observe_tool_result("browser_manage", {"action": "start", "expected_revision": revision},
            "EXACT_FAILURE_RECEIPT", machine_outcome="failed", side_effect_free=True,
            retry_exhausted=True, progress_token="same-state") is None


def test_loop_guard_total_tool_heuristic_never_substitutes_for_explicit_round_budget() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(total_tool_threshold=1)

    guard.observe_tool_call("list_files", {"path": "a"})
    warn = guard.observe_tool_call("list_files", {"path": "b"})
    assert warn is not None and warn.severity == "warn"
    for index in range(20):
        assert guard.observe_tool_call("list_files", {"path": f"after-{index}"}) is None


def test_loop_guard_only_aborts_on_provable_side_effect_free_retry_exhaustion() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(repeated_failure_threshold=2)
    args = {"q": "deploy"}
    err = "[Tool execution error] timeout"

    guard.observe_tool_result(
        "web_search",
        args,
        err,
        machine_outcome="failed",
        side_effect_free=True,
        retry_exhausted=True,
        progress_token="provider-state-v1",
    )
    guard.observe_tool_result(
        "web_search",
        args,
        err,
        machine_outcome="failed",
        side_effect_free=True,
        retry_exhausted=True,
        progress_token="provider-state-v1",
    )
    abort = guard.observe_tool_result(
        "web_search",
        args,
        err,
        machine_outcome="failed",
        side_effect_free=True,
        retry_exhausted=True,
        progress_token="provider-state-v1",
    )

    assert abort is not None
    assert abort.outcome.status == "blocked"
    assert abort.outcome.terminal_reason == "loop_guard"
    assert abort.outcome.next_action == "model_summarize_and_stop"
    assert abort.trace_event["runtime_outcome"]["terminal_reason"] == "loop_guard"
    assert abort.trace_event["proof"]["progress_token"] == "provider-state-v1"


def test_loop_guard_compares_complete_failure_evidence_before_declaring_identical() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(repeated_failure_threshold=2)
    shared_prefix = "[Tool execution error] " + ("same-prefix-" * 100)
    first = shared_prefix + "FIRST_DECISIVE_TAIL"
    second = shared_prefix + "SECOND_DECISIVE_TAIL"

    decisions = [
        guard.observe_tool_result(
            "web_search",
            {"q": "full-evidence"},
            result,
            machine_outcome="failed",
            side_effect_free=True,
            retry_exhausted=True,
            progress_token="provider-state-v1",
        )
        for result in (first, second, first)
    ]

    assert all(decision is None or decision.severity != "abort" for decision in decisions)


def test_loop_guard_never_hard_stops_a_side_effecting_tool() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(repeated_failure_threshold=2)
    for _ in range(8):
        decision = guard.observe_tool_result(
            "send_email",
            {"to": "owner@example.com"},
            "[Tool execution error] timeout",
            machine_outcome="failed",
            side_effect_free=False,
            retry_exhausted=True,
            progress_token="same",
        )
        assert decision is None or decision.severity == "warn"


def test_loop_guard_detects_repeated_tool_failures() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(repeated_failure_threshold=2)
    guard.observe_tool_result("web_search", {"q": "deploy"}, "[Tool execution error] timeout", machine_outcome="failed")
    decision = guard.observe_tool_result(
        "web_search", {"q": "deploy"}, "[Tool execution error] timeout", machine_outcome="failed"
    )

    assert decision is not None
    assert decision.reason == "repeated_tool_failure"
    assert "web_search failed repeatedly with the same result digest" in decision.message
    assert "[Tool execution error] timeout" not in decision.message


def test_loop_guard_detects_repeated_assistant_text() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(repeated_text_threshold=3)
    assert guard.observe_assistant_text("I will check that now.") is None
    assert guard.observe_assistant_text("I will check that now.") is None
    decision = guard.observe_assistant_text("I will check that now.")

    assert decision is not None
    assert decision.reason == "repeated_assistant_text"


def test_loop_guard_allows_many_distinct_successful_tool_calls() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard()

    for index in range(60):
        decision = guard.observe_tool_call(
            "web_fetch",
            {"url": f"https://arxiv.org/abs/2501.{index:05d}", "max_chars": 1500},
        )
        assert decision is None


def test_loop_guard_default_allows_short_retry_burst_for_same_failure() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard()
    args = {"approval_code": "FIN", "status": "PENDING"}
    result = "[Tool execution error] RuntimeError: Feishu query_approval_instances HTTP 400"

    for _ in range(3):
        assert guard.observe_tool_call("feishu_approval_query", args) is None
        assert guard.observe_tool_result("feishu_approval_query", args, result, machine_outcome="failed") is None

    assert guard.observe_tool_call("feishu_approval_query", args) is None
    decision = guard.observe_tool_result("feishu_approval_query", args, result, machine_outcome="failed")

    assert decision is not None
    assert decision.reason == "repeated_tool_failure"


def test_loop_guard_default_identical_tool_args_threshold_is_lenient_but_bounded() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard()

    for _ in range(4):
        assert guard.observe_tool_call("feishu_approval_query", {"approval_code": "FIN"}) is None

    decision = guard.observe_tool_call("feishu_approval_query", {"approval_code": "FIN"})

    assert decision is not None
    assert decision.reason == "identical_tool_args"


def test_loop_guard_warns_on_repeated_provider_cost_pressure() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(cost_pressure_threshold=2, high_tool_schema_tokens=1_000)

    assert (
        guard.observe_provider_call_cost(
            projected_input_tokens=80_000,
            output_tokens=200,
            cache_read_tokens=0,
            tool_schema_tokens=2_000,
        )
        is None
    )
    decision = guard.observe_provider_call_cost(
        projected_input_tokens=81_000,
        output_tokens=200,
        cache_read_tokens=0,
        tool_schema_tokens=2_100,
    )

    assert decision is not None
    assert decision.severity == "warn"
    assert decision.reason == "provider_call_cost_pressure"
    assert decision.trace_event["projected_input_tokens"] == 81_000
    assert decision.trace_event["tool_schema_tokens"] == 2_100
    assert decision.outcome is not None
    assert decision.outcome.next_action == "self_correct_and_continue"


def test_terminal_read_abort_requires_same_task_and_reason():
    import json

    from app.kernel.loop_guard import LoopGuard
    from app.services.runtime_display_status import runtime_failure_observation

    guard = LoopGuard()
    failure = runtime_failure_observation("failed", {"error_code": "provider_error"})
    changed = runtime_failure_observation("failed", {"error_code": "tool_round_limit"})
    first = {"task_id": "first"}
    other = {"task_id": "other"}
    for _ in range(guard.identical_tool_threshold - 1):
        assert guard.observe_tool_result("check_async_task", first, json.dumps({**first, **failure}),
            machine_outcome="succeeded", side_effect_free=True) is None
    # A different reason or a different child cannot complete the same count.
    assert guard.observe_tool_result("check_async_task", first, json.dumps({**first, **changed}),
        machine_outcome="succeeded", side_effect_free=True) is None
    assert guard.observe_tool_result("check_async_task", other, json.dumps({**other, **failure}),
        machine_outcome="succeeded", side_effect_free=True) is None
    stopped = guard.observe_tool_result("check_async_task", first, json.dumps({**first, **failure}),
        machine_outcome="succeeded", side_effect_free=True)
    assert stopped is not None and stopped.severity == "abort"
    assert stopped.reason == "identical_terminal_task_read"
    assert failure["reason"] in stopped.message
    assert guard.failed_tool_calls == 0, "Reading a failed child is still a successful tool read"


def test_pending_read_observations_do_not_abort_or_count_as_terminal_failures():
    import json

    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard()
    for status in ("running", "pending", "completed"):
        for _ in range(guard.identical_tool_threshold + 1):
            assert guard.observe_tool_result("check_async_task", {"task_id": "live"},
                json.dumps({"ok": True, "status": status, "result": None}),
                machine_outcome="succeeded", side_effect_free=True) is None


@pytest.mark.parametrize("mode", ["id_format", "rotation", "pagination", "listing"])
def test_terminal_reads_count_observed_task_across_tools_and_arguments(mode):
    from app.kernel.loop_guard import LoopGuard
    from app.services.runtime_display_status import delegation_failure_result_page, runtime_failure_observation

    guard, task_id = LoopGuard(), uuid4()
    ref = f"runtime-result://{uuid4()}/{'a' * 64}"
    failure = {"task_id": str(task_id), "task_type": "delegation",
        **runtime_failure_observation("failed", {"error_code": "provider_error"})}
    # R16b round 2: read_runtime_result answers with the plain no-body page, so
    # rotation and pagination feed it the page while the task readers keep the
    # coded projection (still produced by pre-deploy replays).
    result_page = delegation_failure_result_page(
        result_ref=ref, task_id=str(task_id), status="failed", metadata={"error_code": "provider_error"}
    )
    for index in range(guard.identical_tool_threshold):
        name, args, body = "check_async_task", {"task_id": str(task_id)}, failure
        if mode == "id_format":
            args["task_id"] = task_id.hex if index % 2 else str(task_id)
            body = {**failure, "task_id": args["task_id"]}
        elif mode == "rotation":
            name = ("check_async_task", "task_output", "read_runtime_result")[index % 3]
            args = {"result_ref": ref} if name == "read_runtime_result" else {"runtime_task_id": task_id.hex}
            body = {"task": failure} if name == "task_output" else (
                result_page if name == "read_runtime_result" else {**failure, "result_ref": ref})
        elif mode == "pagination":
            name, args = "read_runtime_result", {"result_ref": ref, "offset": index, "limit": index + 1,
                "expected_sha256": str(index) * 64}
            body = result_page
        elif mode == "listing":
            name, args, body = "list_async_tasks", {"limit": index + 1}, [failure]
        decision = guard.observe_tool_result(name, args, json.dumps(body),
            machine_outcome="succeeded", side_effect_free=True)
        if index < guard.identical_tool_threshold - 1:
            assert decision is None
        else:
            assert decision is not None and decision.severity == "abort"
            assert decision.reason == "identical_terminal_task_read"
            assert decision.trace_event["identical_terminal_read_count"] == guard.identical_tool_threshold
    assert guard.failed_tool_calls == 0


def test_terminal_reads_keep_different_failed_tasks_separate():
    from app.kernel.loop_guard import LoopGuard
    from app.services.runtime_display_status import runtime_failure_observation

    guard, tasks = LoopGuard(), [uuid4(), uuid4()]
    failure = runtime_failure_observation("failed", {"error_code": "provider_error"})
    for _ in range(guard.identical_tool_threshold - 1):
        for task_id in tasks:
            assert guard.observe_tool_result("list_async_tasks", {},
                json.dumps([{**failure, "task_id": str(task_id), "task_type": "delegation"}]),
                machine_outcome="succeeded", side_effect_free=True) is None


@pytest.mark.parametrize("shape", ["coded", "plain"])
@pytest.mark.parametrize("active_status", ["pending", "running"])
def test_listed_failure_does_not_count_while_another_task_is_active(active_status, shape):
    from app.kernel.loop_guard import LoopGuard
    from app.services.runtime_display_status import delegation_failure_result_page, runtime_failure_observation

    # R16b: list_async_tasks now sends failed entries as the plain page; stored
    # records may still carry the coded observation. Both must wait while
    # another listed task is active.
    def failed_entry(task_id):
        if shape == "plain":
            return delegation_failure_result_page(task_id=task_id, status="failed",
                metadata={"error_code": "provider_error"})
        return {"task_id": task_id, **runtime_failure_observation("failed", {"error_code": "provider_error"})}

    own_id = uuid4()
    guard = LoopGuard(runtime_task_id=own_id.hex)
    tasks = [failed_entry(str(uuid4())),
        {"task_id": str(uuid4()), "task_type": "web_chat_turn", "status": active_status},
        {"task_id": str(own_id), "task_type": "web_chat_turn", "status": "running"}]
    for _ in range(10):
        assert guard.observe_tool_result("list_async_tasks", {}, json.dumps(tasks, ensure_ascii=False),
            machine_outcome="succeeded", side_effect_free=True) is None

    tasks[1] = failed_entry(tasks[1]["task_id"])
    for index in range(guard.identical_tool_threshold):
        decision = guard.observe_tool_result("list_async_tasks", {}, json.dumps(tasks),
            machine_outcome="succeeded", side_effect_free=True)
        if index < guard.identical_tool_threshold - 1:
            assert decision is None
        else:
            assert decision is not None and decision.severity == "abort"
            assert decision.reason == "identical_terminal_task_read"
            assert decision.trace_event["identical_terminal_read_count"] == guard.identical_tool_threshold


@pytest.mark.parametrize("task_type", ["web_chat_turn", "delegation"])
@pytest.mark.parametrize("own_status", ["pending", "running"])
def test_listed_own_run_does_not_block_terminal_counting(task_type, own_status):
    from app.kernel.loop_guard import LoopGuard
    from app.services.runtime_display_status import runtime_failure_observation

    own_id = uuid4()
    guard = LoopGuard(runtime_task_id=str(own_id))
    tasks = [{"task_id": str(uuid4()), **runtime_failure_observation("failed", {"error_code": "provider_error"})},
        {"task_id": own_id.hex, "task_type": task_type, "status": own_status}]
    for index in range(guard.identical_tool_threshold):
        decision = guard.observe_tool_result("list_async_tasks", {}, json.dumps(tasks),
            machine_outcome="succeeded", side_effect_free=True)
        if index < guard.identical_tool_threshold - 1:
            assert decision is None
        else:
            assert decision is not None and decision.severity == "abort"
            assert decision.reason == "identical_terminal_task_read"
            assert decision.trace_event["identical_terminal_read_count"] == guard.identical_tool_threshold


@pytest.mark.parametrize("status", ["skipped", "needs_reconciliation", "killed"])
@pytest.mark.parametrize("task_type", ["delegation", "web_chat_turn"])
def test_terminal_read_stop_uses_neutral_wording(status, task_type):
    from app.kernel.loop_guard import LoopGuard
    from app.services.runtime_display_status import runtime_failure_observation

    guard, task_id = LoopGuard(), uuid4()
    body = {"task_id": str(task_id), "task_type": task_type, **runtime_failure_observation(status, {})}
    for _ in range(guard.identical_tool_threshold):
        decision = guard.observe_tool_result("task_output", {"runtime_task_id": str(task_id)},
            json.dumps({"ok": True, "task": body}), machine_outcome="succeeded", side_effect_free=True)
    assert decision is not None
    assert body["reason"] in decision.message
    assert "同事" not in decision.message and "失败" not in decision.message


@pytest.mark.parametrize("partial", ["PRIVATE_PARTIAL_BODY", {"content": "PRIVATE_PARTIAL_BODY"}])
def test_terminal_read_failure_projection_keeps_only_fixed_runtime_copy(partial):
    from app.services.runtime_display_status import is_runtime_failure_observation, runtime_failure_observation

    failure = runtime_failure_observation("failed", {"error_code": "identical_terminal_task_read"},
        result_summary=partial)
    assert failure["reason"] == "任务结果未变化，已停止本次处理。"
    assert is_runtime_failure_observation(failure)
    assert not is_runtime_failure_observation({**failure, "reason": partial})
