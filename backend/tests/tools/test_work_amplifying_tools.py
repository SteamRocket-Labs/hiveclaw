from __future__ import annotations


def test_work_amplifying_classification_is_decorator_sourced() -> None:
    from app.tools.collector import collect_tools
    from app.tools.registry import (
        is_work_amplifying_tool,
        is_work_amplifying_tool_call,
        work_amplifying_tool_exclusion_names,
        work_amplifying_tool_names,
    )

    collected = collect_tools()
    expected = {
        "spawn_subagent",
        "delegate_to_agent",
        "send_message_to_agent",
        "send_agent_session_message",
        "start_workflow",
        "set_trigger",
        "schedule_wakeup",
        "update_trigger",
        "goal_start",
        "update_goal",
    }

    assert expected <= set(collected.work_amplifying_names)
    assert set(work_amplifying_tool_names()) == set(collected.work_amplifying_names)
    assert all(is_work_amplifying_tool(name) for name in expected)
    assert "schedule_wakeup" not in work_amplifying_tool_exclusion_names()
    assert "update_goal" not in work_amplifying_tool_exclusion_names()
    assert expected - {"schedule_wakeup", "update_goal"} <= set(work_amplifying_tool_exclusion_names())

    assert is_work_amplifying_tool_call("goal_start", {"objective": "finish it"})
    assert is_work_amplifying_tool_call("update_goal", {"status": "active"})
    assert is_work_amplifying_tool_call("update_goal", {"objective": "expanded objective"})
    assert not is_work_amplifying_tool_call("update_goal", {"status": "paused"})
    assert not is_work_amplifying_tool_call("update_goal", {"status": "blocked", "summary": "needs input"})
    assert not is_work_amplifying_tool_call("update_goal", {"status": "complete", "summary": "done"})
    assert not is_work_amplifying_tool_call("update_goal", {"summary": "progress only"})

    # Inspection, cancellation, direct reads, and Team container definition do
    # not themselves expand model work and must remain available in degraded
    # interactive turns.
    for name in ("read_file", "check_subagent", "cancel_trigger", "list_triggers", "team_create"):
        assert not is_work_amplifying_tool(name), name
