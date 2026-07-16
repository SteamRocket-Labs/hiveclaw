"""Centralised Plan Mode read-only tool policy (Functional Core).

Single source of truth for which tools an agent may call while Plan Mode is
active. Referenced by the interactive read-only gate (``tools/service.py``) so
the allowlist no longer drifts as a duplicated inline set (paradigm-convergence
doc §6.4).

Iron law ①: :data:`PLAN_MODE_READONLY_TOOLS` MUST include ``exit_plan_mode``
(the approval exit) — it is the only way for an agent to submit a plan for
confirmation, so it can never be dropped from the allowlist.
"""

from __future__ import annotations

import os

# Read-only context + planning-aid tools permitted while Plan Mode is active.
# Everything else (workspace writes, triggers, delegation, messaging, memory
# writes, command execution, …) is blocked until the user approves the plan.
# Phase 4B will extend the policy to additionally allow writes that target the
# exact provisioned plan file (and never an ``fs_write`` delete).
PLAN_MODE_READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "exit_plan_mode",  # the approval exit — must always remain callable
        "ask_user_question",  # CC-align Phase B: first-class clarification (read-only, asks current user)
        "get_current_time",
        "list_files",
        "read_file",
        "read_context_resource",
        "read_runtime_result",
        "glob_search",
        "grep_search",
        "fs_list",
        "fs_read",
        "web_search",
        "advanced_web_search",
        "anysearch_get_sub_domains",
        "anysearch_search",
        "anysearch_batch_search",
        "anysearch_extract",
        "exa_search",
        "exa_fetch",
        "tavily_search",
        "tavily_extract",
        "firecrawl_search",
        "web_fetch",
        "advanced_web_fetch",
        "firecrawl_fetch",
        "xcrawl_scrape",
        "search_memory",
        "load_memory",
        "list_triggers",
        "tool_search",
        "load_skill",
        # CC parity: Plan Mode can inspect deterministic workflow shape and
        # read background subagent status, but may not start execution.
        "propose_dynamic_workflow",
        "preview_workflow",
        "check_subagent",
        # CC parity: TodoWrite is allowed in plan mode — the work ledger is the
        # agent's private working memory (internal scratchpad), not an external or
        # workspace mutation. Hive's track_todo/record_finding/read_ledger are the
        # equivalent, so the agent can organize its planning while exploring.
        "track_todo",
        "record_finding",
        "read_ledger",
    }
)


# Workspace-write tools that Phase 4B may allow — but ONLY when they target the
# exact provisioned plan file. Never widened to a directory-level whitelist.
_PLAN_FILE_WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "fs_write"})
# Argument names different write tools use for their target path.
_PATH_ARG_NAMES = ("path", "file_path", "filename")
_PLAN_MODE_READONLY_SUBAGENT_TYPES: frozenset[str] = frozenset({"explorer", "critic"})


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _extract_target_path(arguments: dict) -> str | None:
    for key in _PATH_ARG_NAMES:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_exact_plan_file_write(tool_name: str, arguments: dict, plan_file_path: str | None) -> bool:
    """True only for a write that targets the exact provisioned plan file.

    Enforces the Phase 4B iron laws: no plan file → no write; ``fs_write``
    delete is never allowed; the target must normalise (resolving ``./`` and
    ``..``) to exactly ``plan_file_path`` — no traversal escape, no directory
    wildcard.
    """
    if not plan_file_path:
        return False
    if tool_name == "fs_write" and str(arguments.get("mode") or "write").lower() == "delete":
        return False
    target = _extract_target_path(arguments)
    if not target:
        return False
    return os.path.normpath(target) == os.path.normpath(plan_file_path)


def _is_plan_mode_readonly_subagent_spawn(arguments: dict | None) -> bool:
    """Allow only the CC-style read-only exploration lane in Plan Mode.

    ``spawn_subagent`` is normally sensitive because it can start workers,
    background runs, ledger ownership, persistent definitions, and subagent
    memory writeback. Inside Plan Mode the only permitted shape is an inline,
    synchronous explorer/critic used to gather evidence for the plan.
    """
    args = arguments or {}
    if not isinstance(args, dict):
        return False
    prompt = str(args.get("prompt") or args.get("task") or "").strip()
    if not prompt:
        return False
    subagent_type = str(args.get("subagent_type") or args.get("type") or "general-purpose").strip()
    if not subagent_type:
        subagent_type = "general-purpose"
    if subagent_type not in _PLAN_MODE_READONLY_SUBAGENT_TYPES:
        return False
    if str(args.get("definition_name") or "").strip():
        return False
    if str(args.get("team_name") or "").strip():
        return False
    if str(args.get("mode") or "").strip():
        return False
    if str(args.get("isolation") or "").strip() == "all":
        return False
    if str(args.get("ledger_todo_id") or "").strip():
        return False
    if _truthy(args.get("run_in_background")):
        return False
    return True


def is_plan_mode_tool_allowed(
    tool_name: str,
    arguments: dict | None = None,
    plan_file_path: str | None = None,
) -> bool:
    """Return ``True`` if ``tool_name`` may run while Plan Mode is active.

    Read-only / planning-aid tools are always allowed. Workspace-write tools are
    blocked unless a ``plan_file_path`` has been provisioned (Phase 4B) AND the
    call targets that exact normalised path — and never for ``fs_write`` delete.
    Phase 3 callers pass no ``plan_file_path``, so every write stays blocked.
    """
    if tool_name == "spawn_subagent":
        return _is_plan_mode_readonly_subagent_spawn(arguments)
    if tool_name in PLAN_MODE_READONLY_TOOLS:
        return True
    if tool_name in _PLAN_FILE_WRITE_TOOLS:
        return _is_exact_plan_file_write(tool_name, arguments or {}, plan_file_path)
    return False


__all__ = ["PLAN_MODE_READONLY_TOOLS", "is_plan_mode_tool_allowed"]
