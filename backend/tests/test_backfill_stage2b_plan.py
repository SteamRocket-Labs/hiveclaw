"""Functional-core checks on the stage-2b backfill plan (no DB)."""

from __future__ import annotations

from app.scripts.backfill_stage2b_tenant_id import BACKFILL_PLAN


def test_plan_covers_19_distinct_tables():
    assert len(BACKFILL_PLAN) == 19
    assert len({s.table for s in BACKFILL_PLAN}) == 19


def test_task_logs_backfilled_after_tasks():
    """task_logs derives tenant from tasks → tasks MUST come first in the plan."""
    order = [s.table for s in BACKFILL_PLAN]
    assert order.index("tasks") < order.index("task_logs")


def test_special_and_standard_sources():
    by_table = {s.table: s for s in BACKFILL_PLAN}
    # runtime_tasks has no agent_id — derives from parent_agent_id → agents
    assert by_table["runtime_tasks"].source_table == "agents"
    assert by_table["runtime_tasks"].local_fk == "parent_agent_id"
    # task_logs has no agent_id — derives from task_id → tasks
    assert by_table["task_logs"].source_table == "tasks"
    assert by_table["task_logs"].local_fk == "task_id"
    # a representative standard table derives from agent_id → agents
    assert by_table["chat_messages"].source_table == "agents"
    assert by_table["chat_messages"].local_fk == "agent_id"
    assert by_table["agent_plan_requests"].source_table == "agents"
    assert by_table["agent_plan_requests"].local_fk == "agent_id"
