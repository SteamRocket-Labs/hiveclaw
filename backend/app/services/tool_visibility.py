"""Shared rules for per-agent tool visibility."""

from __future__ import annotations

from typing import Any

HR_AGENT_NAME = "__system_hr__"
HR_AGENT_CLASS = "internal_system"
HR_ONLY_TOOL_NAMES = frozenset({"create_digital_employee", "preview_agent_blueprint"})


def is_hr_agent(agent: Any | None) -> bool:
    """Return True only for the tenant's dedicated HR onboarding agent."""
    if agent is None:
        return False
    return getattr(agent, "agent_class", None) == HR_AGENT_CLASS and getattr(agent, "name", None) == HR_AGENT_NAME


def is_tool_allowed_for_agent(tool: Any, agent: Any | None) -> bool:
    """Enforce tools that are reserved for a specific system agent."""
    if getattr(tool, "name", None) in HR_ONLY_TOOL_NAMES:
        return is_hr_agent(agent)
    return True
