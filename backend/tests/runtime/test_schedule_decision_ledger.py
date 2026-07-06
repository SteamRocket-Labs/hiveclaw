from __future__ import annotations


def test_build_schedule_decision_entry_links_command_plan_trigger_and_run() -> None:
    from app.runtime.schedule_decision_ledger import build_schedule_decision_entry

    entry = build_schedule_decision_entry(
        command_origin="/schedule",
        natural_vs_structured="structured",
        plan_gate_decision={"allowed": True, "reason": "confirmed_plan_handoff"},
        confirmed_plan_ref={"plan_id": "plan-1", "plan_version": 2, "plan_hash": "abc"},
        trigger_id="trigger-1",
        next_fire="2026-07-07T00:00:00+00:00",
        runtime_task_id="run-1",
    )

    assert entry["schema"] == "hive.ccplus.schedule_decision.v1"
    assert entry["command_origin"] == "/schedule"
    assert entry["natural_vs_structured"] == "structured"
    assert entry["plan_gate_decision"]["reason"] == "confirmed_plan_handoff"
    assert entry["confirmed_plan_ref"]["plan_id"] == "plan-1"
    assert entry["trigger_id"] == "trigger-1"
    assert entry["next_fire"] == "2026-07-07T00:00:00+00:00"
    assert entry["runtime_task_id"] == "run-1"
