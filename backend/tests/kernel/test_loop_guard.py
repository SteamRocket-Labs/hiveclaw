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


def test_loop_guard_detects_repeated_tool_failures() -> None:
    from app.kernel.loop_guard import LoopGuard

    guard = LoopGuard(failed_tool_threshold=2)
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

