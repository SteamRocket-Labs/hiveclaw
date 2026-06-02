"""Phase 3 contract: centralised Plan Mode read-only tool policy.

Single source of truth for which tools an agent may call while Plan Mode is
active. Replaces the drift-prone duplicated allowlist that lived inline in
tools/service.py (paradigm-convergence doc §6.4).

Iron law ①: this set MUST include exit_plan_mode (the approval exit) and MUST
NOT be derived from PLANNER_ALLOWED_TOOLS, which omits it — reusing that set
would silently remove the only way to submit a plan.
"""

from __future__ import annotations

from app.tools.plan_mode_policy import PLAN_MODE_READONLY_TOOLS, is_plan_mode_tool_allowed


def test_exit_plan_mode_is_always_allowed():
    assert is_plan_mode_tool_allowed("exit_plan_mode") is True
    assert "exit_plan_mode" in PLAN_MODE_READONLY_TOOLS


def test_readonly_and_planning_aid_tools_allowed():
    for name in (
        "read_file",
        "list_files",
        "glob_search",
        "grep_search",
        "fs_read",
        "fs_list",
        "web_search",
        "web_fetch",
        "firecrawl_fetch",
        "xcrawl_scrape",
        "search_memory",
        "load_memory",
        "list_triggers",
        "list_objectives",
        "get_current_time",
        "tool_search",
        "load_skill",
    ):
        assert is_plan_mode_tool_allowed(name) is True, name


def test_write_send_and_autonomous_tools_blocked():
    # Phase 3 keeps ALL workspace writes and side-effecting tools blocked.
    for name in (
        "write_file",
        "edit_file",
        "fs_write",
        "set_trigger",
        "update_trigger",
        "delegate_to_agent",
        "manage_tasks",
        "send_feishu_message",
        "save_memory",
        "create_digital_employee",
    ):
        assert is_plan_mode_tool_allowed(name) is False, name


def test_unknown_tool_is_blocked_by_default():
    assert is_plan_mode_tool_allowed("totally_unknown_tool") is False


def test_policy_is_not_derived_from_planner_allowlist():
    # Iron law ①: prove we did not reuse PLANNER_ALLOWED_TOOLS (which lacks the
    # exit_plan_mode exit). If a future refactor aliases the two, this fails.
    from app.services.agent_plan_planner import PLANNER_ALLOWED_TOOLS

    assert "exit_plan_mode" not in PLANNER_ALLOWED_TOOLS
    assert "exit_plan_mode" in PLAN_MODE_READONLY_TOOLS
