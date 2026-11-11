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


def test_failed_colleague_observation_matches_pre_deploy_bytes():
    payload = runtime_failure_observation("failed", {"error_code": "tool_budget"})
    # Exact 83984c30 projection, including field order for stored body hashes.
    assert json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() == (
        '{"ok":true,"status":"failed","reason_code":"tool_budget",'
        '"reason":"这次用完了用量预算，没有完成。","retryable":false}'
    ).encode()


def test_failed_return_context_instructs_member_requested_retry():
    from app.services.agent_session_continuation import build_result_integration_runtime_context

    instruction = "Do not delegate again unless the member asks."
    failed = {"items": [{"task_type": "a2a_delegation", "terminal_status": "failed"}]}
    assert instruction in build_result_integration_runtime_context(failed)
    assert instruction not in build_result_integration_runtime_context({"items": []})


@pytest.mark.parametrize("reader", ["check_async_task", "task_output", "list_async_tasks"])
@pytest.mark.parametrize("task_type", ["trigger", "workflow"])
def test_running_non_colleague_polls_do_not_end_turn(reader, task_type):
    guard = LoopGuard()
    task_id = str(uuid4())
    for index in range(guard.identical_tool_threshold):
        body = {"task_id": task_id, "task_type": task_type, "status": "running", "result": None}
        if reader == "task_output":
            body = {"ok": True, "task": body}
        elif reader == "list_async_tasks":
            body = [body]
        assert guard.observe_tool_result(reader, {"cursor": index}, json.dumps(body),
            machine_outcome="succeeded", side_effect_free=True) is None


@pytest.mark.parametrize("task_type", ["trigger", "workflow", "delegation"])
async def test_check_async_task_exposes_persisted_task_type(monkeypatch, task_type):
    from app.agents import orchestrator

    task_id, parent_id = str(uuid4()), uuid4()

    async def persisted(requested_id):
        assert requested_id == task_id
        return {"task_id": task_id, "task_type": task_type, "status": "running",
            "parent_agent_id": str(parent_id), "result": None}

    monkeypatch.setattr(orchestrator, "get_runtime_task_record", persisted)
    payload = await orchestrator.check_async_delegation(task_id, parent_agent_id=parent_id)
    assert payload["task_type"] == task_type
    guard = LoopGuard()
    for index in range(guard.identical_tool_threshold):
        decision = guard.observe_tool_result("check_async_task", {"cursor": index}, json.dumps(payload),
            machine_outcome="succeeded", side_effect_free=True)
        assert (decision is not None) == (task_type == "delegation" and index == guard.identical_tool_threshold - 1)
    if task_type == "delegation":
        assert decision.message == "同事正在处理，结果回来后会在这里继续。"
