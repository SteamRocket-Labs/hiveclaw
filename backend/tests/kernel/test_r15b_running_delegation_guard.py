"""Use the existing read counter and threshold for running colleagues as well."""

import json
from uuid import uuid4

import pytest

from app.kernel.loop_guard import LoopGuard
from app.services.runtime_display_status import runtime_failure_observation


@pytest.mark.parametrize("reader", ["check_async_task", "task_output", "list_async_tasks"])
def test_running_colleague_stops_at_the_existing_identical_read_threshold(reader):
    own, child = uuid4(), uuid4()
    guard = LoopGuard(runtime_task_id=own)
    for index in range(guard.identical_tool_threshold):
        body = {"task_id": child.hex if index % 2 else str(child), "task_type": "delegation",
            "status": "pending" if index % 2 else "running", "result": None}
        if reader == "task_output":
            body = {"ok": True, "task": body}
        elif reader == "list_async_tasks":
            body = [{"task_id": str(own), "status": "running", "task_type": "web_chat_turn"}, body]
        decision = guard.observe_tool_result(reader, {"cursor": index}, json.dumps(body),
            machine_outcome="succeeded", side_effect_free=True)
        if index < guard.identical_tool_threshold - 1:
            assert decision is None
        else:
            assert decision is not None and decision.severity == "abort"
            assert decision.message == "同事正在处理，结果回来后会在这里继续。"
    assert guard.failed_tool_calls == 0


def test_unrelated_running_tasks_and_completed_colleagues_do_not_stop_the_turn():
    guard = LoopGuard()
    for status, task_type in [("running", "web_chat_turn"), ("completed", "delegation")]:
        for _ in range(guard.identical_tool_threshold + 1):
            assert guard.observe_tool_result("task_output", {}, json.dumps({"ok": True, "task": {
                "task_id": str(uuid4()), "task_type": task_type, "status": status}}),
                machine_outcome="succeeded", side_effect_free=True) is None


@pytest.mark.parametrize("tool", ["delegate_to_agent", "send_message_to_agent", "send_agent_session_message"])
def test_failed_colleague_retry_is_denied_but_a_new_turn_can_delegate(tool):
    guard = LoopGuard()
    payload = {"task_id": str(uuid4()), "task_type": "delegation",
        **runtime_failure_observation("failed", {"error_code": "tool_budget"})}
    guard.observe_tool_result("check_async_task", {}, json.dumps(payload),
        machine_outcome="succeeded", side_effect_free=True)
    # Check at the effect boundary, including another tool in the same model batch.
    decision = guard.before_tool_effect(tool)
    assert decision is not None and decision.severity == "abort"
    assert decision.message == "同事这次没有完成：这次用完了用量预算，没有完成。需要重试的话，请告诉我。"
    assert guard.before_tool_effect("read_file") is None
    assert LoopGuard().before_tool_effect(tool) is None


def test_failed_return_context_is_cleared_before_the_members_new_turn():
    from types import SimpleNamespace
    from app.services.web_chat_run_orchestrator import _copy_optional_runtime_metadata

    manifest = {"items": [{"task_type": "a2a_delegation", "terminal_status": "failed"}]}
    context = SimpleNamespace(metadata={})
    state = SimpleNamespace(metadata={"result_manifest": manifest}, runtime_session_context=context)
    _copy_optional_runtime_metadata(state)
    guard = LoopGuard()
    guard.observe_result_manifest(context.metadata["result_manifest"])
    assert guard.before_tool_effect("delegate_to_agent") is not None
    state.metadata = {"latest_user_prompt_overrides_history": True}
    _copy_optional_runtime_metadata(state)
    assert "result_manifest" not in context.metadata
    assert LoopGuard().before_tool_effect("delegate_to_agent") is None
