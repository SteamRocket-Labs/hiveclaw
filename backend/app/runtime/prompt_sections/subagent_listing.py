"""Prompt-facing listing for session-local subagent worker types."""

from __future__ import annotations

from app.agents.subagent import (
    SUBAGENT_TYPE_CRITIC,
    SUBAGENT_TYPE_EXPLORER,
    SUBAGENT_TYPE_GENERAL_PURPOSE,
    SUBAGENT_TYPE_WORKER,
    builtin_type_description,
)

_BUILTIN_ORDER = (
    SUBAGENT_TYPE_GENERAL_PURPOSE,
    SUBAGENT_TYPE_EXPLORER,
    SUBAGENT_TYPE_WORKER,
    SUBAGENT_TYPE_CRITIC,
)


def build_subagent_listing_section() -> str:
    """Render the always-visible session worker type list.

    This mirrors CC's persistent agent-type routing signal without adding a
    second execution path: every entry routes to the same ``spawn_subagent``
    tool, while real digital-employee collaboration remains on A2A tools.
    """

    lines = [
        "## Session Worker Types",
        "",
        "These are To Session Worker types for `spawn_subagent`; they are not A2A employees and do not require A2A Collaborators.",
        "Use `prompt` for the worker instruction and `subagent_type` to choose the type.",
        "",
    ]
    for name in _BUILTIN_ORDER:
        description = builtin_type_description(name)
        lines.append(f"- `{name}`: {description}")
    return "\n".join(lines)
