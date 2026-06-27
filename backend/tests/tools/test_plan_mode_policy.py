"""Phase 3 contract: centralised Plan Mode read-only tool policy.

Single source of truth for which tools an agent may call while Plan Mode is
active. Replaces the drift-prone duplicated allowlist that lived inline in
tools/service.py (paradigm-convergence doc §6.4).

Iron law ①: this set MUST include exit_plan_mode (the approval exit) — it is the
only way to submit a plan for confirmation, so it can never be dropped.
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


def test_planning_ledger_tools_allowed_in_plan_mode():
    # CC parity: TodoWrite is allowed in plan mode. The work ledger is the agent's
    # private working memory (scratchpad), not an external/workspace mutation, so
    # the agent can organize its planning while exploring read-only.
    for name in ("track_todo", "record_finding", "read_ledger"):
        assert is_plan_mode_tool_allowed(name) is True, name
        assert name in PLAN_MODE_READONLY_TOOLS


def test_plan_mode_allows_readonly_subagent_and_workflow_inspection_tools():
    # CC parity: Plan Mode can use read-only specialist exploration/planning
    # helpers, but not execution or mutation.
    assert is_plan_mode_tool_allowed("preview_workflow") is True
    assert is_plan_mode_tool_allowed("check_subagent") is True
    assert is_plan_mode_tool_allowed("spawn_subagent", {"prompt": "inspect current state", "type": "explorer"}) is True
    assert is_plan_mode_tool_allowed("spawn_subagent", {"prompt": "verify claim", "subagent_type": "critic"}) is True


def test_plan_mode_blocks_mutating_or_durable_subagent_spawns():
    # The allowed Plan Mode subagent lane is narrow: synchronous inline
    # explorer/critic only. Workers, background runs, persistent definitions, and
    # ledger ownership all create execution/durable side effects and must stay
    # blocked until the plan is approved.
    blocked_payloads = (
        {"prompt": "default worker now maps to general-purpose"},
        {"task": "edit this", "type": "worker"},
        {"prompt": "edit this", "subagent_type": "general-purpose"},
        {"task": "inspect later", "run_in_background": True},
        {"task": "use custom helper", "definition_name": "team-scout"},
        {"task": "take todo", "ledger_todo_id": "todo-1"},
    )
    for payload in blocked_payloads:
        assert is_plan_mode_tool_allowed("spawn_subagent", payload) is False, payload


def test_exit_plan_mode_is_always_in_the_readonly_allowlist():
    # Iron law ①: exit_plan_mode (the approval exit) MUST stay in the read-only
    # allowlist, otherwise the agent could never submit a plan from Plan Mode.
    # (The old RPC PLANNER_ALLOWED_TOOLS set that this used to contrast against
    # was removed in path-unification cut ④; the read-only policy is now the only
    # plan-mode tool allowlist.)
    assert "exit_plan_mode" in PLAN_MODE_READONLY_TOOLS


# ── Phase 4B: exact plan-file write whitelist ──

_PLAN_FILE = "workspace/plans/s1.plan.md"


def test_write_tools_blocked_without_a_provisioned_plan_file():
    # Phase 3 behaviour preserved: no plan_file_path → every write is blocked.
    assert is_plan_mode_tool_allowed("write_file", {"path": _PLAN_FILE}) is False
    assert is_plan_mode_tool_allowed("write_file", {"path": _PLAN_FILE}, None) is False
    assert is_plan_mode_tool_allowed("fs_write", {"path": _PLAN_FILE, "mode": "write"}) is False


def test_write_to_the_exact_plan_file_is_allowed():
    assert is_plan_mode_tool_allowed("write_file", {"path": _PLAN_FILE}, _PLAN_FILE) is True
    assert is_plan_mode_tool_allowed("edit_file", {"path": _PLAN_FILE}, _PLAN_FILE) is True
    assert is_plan_mode_tool_allowed("fs_write", {"path": _PLAN_FILE, "mode": "write"}, _PLAN_FILE) is True
    assert is_plan_mode_tool_allowed("fs_write", {"path": _PLAN_FILE, "mode": "edit"}, _PLAN_FILE) is True
    # Accept the alternate path argument name used by some write tools.
    assert is_plan_mode_tool_allowed("write_file", {"file_path": _PLAN_FILE}, _PLAN_FILE) is True


def test_write_to_any_other_path_blocked_even_with_a_plan_file():
    assert is_plan_mode_tool_allowed("write_file", {"path": "workspace/secret.md"}, _PLAN_FILE) is False
    assert is_plan_mode_tool_allowed("write_file", {"path": "soul.md"}, _PLAN_FILE) is False


def test_path_traversal_is_normalised_then_blocked():
    # ".." escaping the plan file resolves elsewhere → blocked.
    assert is_plan_mode_tool_allowed("write_file", {"path": "workspace/plans/../secret.md"}, _PLAN_FILE) is False
    # A redundant "./" that still resolves to the exact plan file is allowed.
    assert is_plan_mode_tool_allowed("write_file", {"path": "workspace/plans/./s1.plan.md"}, _PLAN_FILE) is True


def test_fs_write_delete_is_always_blocked_even_on_the_plan_file():
    # Iron law ③: delete is never permitted in Plan Mode, even on the plan file.
    assert is_plan_mode_tool_allowed("fs_write", {"path": _PLAN_FILE, "mode": "delete"}, _PLAN_FILE) is False


def test_directory_wildcard_is_not_a_valid_plan_file_target():
    # Iron law: no directory-level whitelist — only the exact file matches.
    assert is_plan_mode_tool_allowed("write_file", {"path": "workspace/plans/other.md"}, _PLAN_FILE) is False
    assert is_plan_mode_tool_allowed("write_file", {"path": "workspace/plans"}, _PLAN_FILE) is False
