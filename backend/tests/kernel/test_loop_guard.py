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


# ── A4: warn-before-abort (docs/agent-lifecycle-cc-alignment.md 主题 A) ──
# CC philosophy (doc §12.2): soft constraints first — give the model a
# diagnostic + self-correction chance before the hard stop.


def test_loop_guard_warns_before_abort_on_identical_args() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(identical_tool_threshold=3)  # warn at 3, abort at ceil(3*1.5)=5

    assert guard.observe_tool_call("list_files", {"path": "."}) is None
    assert guard.observe_tool_call("list_files", {"path": "."}) is None

    warn = guard.observe_tool_call("list_files", {"path": "."})
    assert warn is not None
    assert warn.severity == "warn"
    assert warn.trace_event["event"] == "loop_guard_warning"

    # Between warn and abort thresholds: no duplicate warning for the same pattern
    assert guard.observe_tool_call("list_files", {"path": "."}) is None

    abort = guard.observe_tool_call("list_files", {"path": "."})
    assert abort is not None
    assert abort.severity == "abort"
    assert abort.trace_event["event"] == "loop_guard_triggered"


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


def test_loop_guard_abort_decisions_keep_severity_abort() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(repeated_failure_threshold=2)  # warn 2, abort ceil(3)=3
    args = {"q": "x"}
    err = "[Tool execution error] timeout"
    guard.observe_tool_result("web_search", args, err)
    warn = guard.observe_tool_result("web_search", args, err)
    assert warn is not None and warn.severity == "warn"

    abort = guard.observe_tool_result("web_search", args, err)
    assert abort is not None and abort.severity == "abort"


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
