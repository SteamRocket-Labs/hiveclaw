"""Tests for the Plan-Mode tool gate registry (docs/plan-mode-design.md §9.2).

The registry is the read side of the ``ToolMeta.plan_gate_action_kind`` tag: it
scans the decorator registry (the canonical, code-level governance fact) and
tells the :class:`ToolRuntimeService` gate which tools to hard-block vs which
self-gate. These tests pin the §9.2 first-batch contract so a future edit that
forgets to tag (or mistags) an autonomous-enabling tool fails loudly.
"""

from __future__ import annotations

import pytest

from app.services.plan_mode_core import ACTION_KINDS


def test_first_batch_tools_are_tagged_with_expected_action_kinds():
    """The §9.2 first-batch autonomous-enabling tools carry the right tag.

    The hard-gated tools map onto a real ACTION_KIND; deep_research_start
    is registered via the BRIDGE_SELF sentinel (it owns its plan_confirmed gate).
    """
    from app.tools.plan_gate_registry import BRIDGE_SELF, plan_gated_tool_action_kinds

    tagged = plan_gated_tool_action_kinds()

    assert tagged["set_trigger"] == "create_enabled_trigger"
    assert tagged["update_trigger"] == "create_enabled_trigger"
    assert tagged["delegate_to_agent"] == "start_delegation"
    assert tagged["manage_tasks"] == "start_long_task"
    assert "create_digital_employee" not in tagged
    assert tagged["deep_research_start"] == BRIDGE_SELF


def test_hard_gated_tools_resolve_to_a_real_action_kind():
    from app.tools.plan_gate_registry import hard_gated_action_kind

    assert hard_gated_action_kind("set_trigger") == "create_enabled_trigger"
    assert hard_gated_action_kind("update_trigger") == "create_enabled_trigger"
    assert hard_gated_action_kind("delegate_to_agent") == "start_delegation"
    assert hard_gated_action_kind("manage_tasks") == "start_long_task"
    # Every hard-gated action_kind must be a member of the canonical ACTION_KINDS.
    for tool_name in ("set_trigger", "update_trigger", "delegate_to_agent", "manage_tasks"):
        assert hard_gated_action_kind(tool_name) in ACTION_KINDS


def test_manage_tasks_only_hard_gates_auto_executing_todo_create():
    from app.tools.plan_gate_registry import hard_gated_action_kind

    assert hard_gated_action_kind("manage_tasks", {"action": "create", "task_type": "todo"}) == "start_long_task"
    # Missing task_type defaults to todo in the handler, so it is also auto-executing.
    assert hard_gated_action_kind("manage_tasks", {"action": "create"}) == "start_long_task"
    assert hard_gated_action_kind("manage_tasks", {"action": "create", "task_type": "supervision"}) is None
    assert hard_gated_action_kind("manage_tasks", {"action": "update_status", "status": "done"}) is None
    assert hard_gated_action_kind("manage_tasks", {"action": "delete"}) is None


def test_bridge_self_tools_are_not_hard_gated():
    """deep_research_start is registered but self-gates — the service must not
    invoke PlanModeGate for it (that would double-block its plan_confirmed)."""
    from app.tools.plan_gate_registry import hard_gated_action_kind

    assert hard_gated_action_kind("deep_research_start") is None


def test_low_risk_sync_tools_are_not_plan_gated():
    """§9.2: low-risk current-turn synchronous actions must NOT enter Plan Mode."""
    from app.tools.plan_gate_registry import hard_gated_action_kind, plan_gated_tool_action_kinds

    tagged = plan_gated_tool_action_kinds()
    for tool_name in ("write_file", "edit_file", "delete_file", "send_feishu_message", "read_file", "web_search"):
        assert tool_name not in tagged
        assert hard_gated_action_kind(tool_name) is None


@pytest.mark.parametrize("tool_name", ["cancel_trigger", "list_triggers"])
def test_trigger_teardown_and_read_tools_are_not_gated(tool_name):
    """Cancelling/listing triggers is not an autonomous-enabling action."""
    from app.tools.plan_gate_registry import hard_gated_action_kind

    assert hard_gated_action_kind(tool_name) is None
