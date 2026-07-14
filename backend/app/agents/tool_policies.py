"""Shared child-agent tool-surface policies.

Child sessions inherit assigned capabilities and are narrowed by delegated
authority plus an explicitly selected execution profile. The base exclusion
contains only human-facing interactions a child cannot truthfully complete.
"""

from __future__ import annotations

DELEGATED_WORKER_BASE_EXCLUDED_TOOLS: tuple[str, ...] = (
    "ask_user_question",
    "request_plan_mode",
)
