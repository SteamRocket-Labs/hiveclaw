from __future__ import annotations


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
    guard.observe_tool_result("web_search", args, err)
    warn = guard.observe_tool_result("web_search", args, err)
    assert warn is not None and warn.severity == "warn"

    for _ in range(10):
        assert guard.observe_tool_result("web_search", args, err) is None


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
        side_effect_free=True,
        retry_exhausted=True,
        progress_token="provider-state-v1",
    )
    guard.observe_tool_result(
        "web_search",
        args,
        err,
        side_effect_free=True,
        retry_exhausted=True,
        progress_token="provider-state-v1",
    )
    abort = guard.observe_tool_result(
        "web_search",
        args,
        err,
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
            side_effect_free=False,
            retry_exhausted=True,
            progress_token="same",
        )
        assert decision is None or decision.severity == "warn"


def test_loop_guard_detects_repeated_tool_failures() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(repeated_failure_threshold=2)
    guard.observe_tool_result("web_search", {"q": "deploy"}, "[Tool execution error] timeout")
    decision = guard.observe_tool_result("web_search", {"q": "deploy"}, "[Tool execution error] timeout")

    assert decision is not None
    assert decision.reason == "repeated_tool_failure"
    assert "timeout" in decision.message


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
        assert guard.observe_tool_result("feishu_approval_query", args, result) is None

    assert guard.observe_tool_call("feishu_approval_query", args) is None
    decision = guard.observe_tool_result("feishu_approval_query", args, result)

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
