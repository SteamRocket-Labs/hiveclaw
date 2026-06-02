"""Centralised Plan Mode read-only tool policy (Functional Core).

Single source of truth for which tools an agent may call while Plan Mode is
active. Referenced by the interactive read-only gate (``tools/service.py``) so
the allowlist no longer drifts as a duplicated inline set (paradigm-convergence
doc §6.4).

Iron law ①: :data:`PLAN_MODE_READONLY_TOOLS` includes ``exit_plan_mode`` (the
approval exit). Do NOT derive this set from ``PLANNER_ALLOWED_TOOLS`` — that set
omits ``exit_plan_mode``, and reusing it would remove the only way to submit a
plan for confirmation.
"""

from __future__ import annotations

# Read-only context + planning-aid tools permitted while Plan Mode is active.
# Everything else (workspace writes, triggers, delegation, messaging, memory
# writes, command execution, …) is blocked until the user approves the plan.
# Phase 4B will extend the policy to additionally allow writes that target the
# exact provisioned plan file (and never an ``fs_write`` delete).
PLAN_MODE_READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "exit_plan_mode",  # the approval exit — must always remain callable
        "get_current_time",
        "list_files",
        "read_file",
        "glob_search",
        "grep_search",
        "fs_list",
        "fs_read",
        "web_search",
        "web_fetch",
        "firecrawl_fetch",
        "xcrawl_scrape",
        "search_memory",
        "load_memory",
        "list_triggers",
        "list_objectives",
        "tool_search",
        "load_skill",
    }
)


def is_plan_mode_tool_allowed(tool_name: str) -> bool:
    """Return ``True`` if ``tool_name`` may run while Plan Mode is active.

    Phase 3 allows only the read-only / planning-aid set; every side-effecting
    tool is blocked. Phase 4B will widen this to also permit writes that target
    the exact provisioned plan file (and will keep ``fs_write`` delete blocked).
    """
    return tool_name in PLAN_MODE_READONLY_TOOLS


__all__ = ["PLAN_MODE_READONLY_TOOLS", "is_plan_mode_tool_allowed"]
