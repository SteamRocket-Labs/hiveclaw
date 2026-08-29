"""Shared rules for per-agent tool visibility."""

from __future__ import annotations

from typing import Any

HR_AGENT_NAME = "__system_hr__"
HR_AGENT_CLASS = "internal_system"
HR_ONLY_TOOL_NAMES = frozenset({"create_digital_employee", "preview_agent_blueprint"})
REGULAR_AGENT_ONLY_TOOL_NAMES = frozenset({"start_hr_agent_handoff"})


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            return str(name) if name else None
    name = getattr(tool, "name", None)
    return str(name) if name else None


def is_hr_agent(agent: Any | None) -> bool:
    """Return True only for the tenant's dedicated HR onboarding agent."""
    if agent is None:
        return False
    return getattr(agent, "agent_class", None) == HR_AGENT_CLASS and getattr(agent, "name", None) == HR_AGENT_NAME


def is_tool_allowed_for_agent(tool: Any, agent: Any | None) -> bool:
    """Enforce tools that are reserved for a specific system agent."""
    name = _tool_name(tool)
    if name in HR_ONLY_TOOL_NAMES:
        return is_hr_agent(agent)
    if name in REGULAR_AGENT_ONLY_TOOL_NAMES:
        return not is_hr_agent(agent)
    return True
