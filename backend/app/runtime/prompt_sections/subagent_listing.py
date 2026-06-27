"""Prompt-facing listing for session-local subagent worker types."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.subagent import (
    SUBAGENT_TYPE_CRITIC,
    SUBAGENT_TYPE_EXPLORER,
    SUBAGENT_TYPE_GENERAL_PURPOSE,
    SUBAGENT_TYPE_WORKER,
    builtin_type_description,
)
from app.agents.subagent_definition import SCOPE_BUILTIN, list_subagent_definitions

_BUILTIN_ORDER = (
    SUBAGENT_TYPE_GENERAL_PURPOSE,
    SUBAGENT_TYPE_EXPLORER,
    SUBAGENT_TYPE_WORKER,
    SUBAGENT_TYPE_CRITIC,
)


def _render_custom_definition_rows(
    *,
    agent_id: Any | None,
    tenant_id: Any | None,
    agent_data_dir: Path | str | None,
) -> list[str]:
    if agent_id is None and tenant_id is None:
        return []
    rows = [
        row
        for row in list_subagent_definitions(agent_id=agent_id, tenant_id=tenant_id, agent_data_dir=agent_data_dir)
        if row.get("scope") != SCOPE_BUILTIN
    ]
    if not rows:
        return []
    lines = [
        "",
        "### Custom Session Worker Definitions",
        "Use these with the same `spawn_subagent` tool by setting `definition_name`; do not use A2A delegation for them.",
        "",
    ]
    for row in rows:
        name = str(row.get("name") or "")
        scope = str(row.get("scope") or "custom")
        worker_type = str(row.get("type") or "general-purpose")
        description = str(row.get("description") or "")
        extra: list[str] = []
        max_rounds = row.get("max_tool_rounds")
        if max_rounds:
            extra.append(f"max_tool_rounds={max_rounds}")
        allowed_tools = row.get("allowed_tools")
        if isinstance(allowed_tools, list) and allowed_tools:
            extra.append("tools=" + ",".join(str(tool) for tool in allowed_tools[:6]))
        suffix = f" [{'; '.join(extra)}]" if extra else ""
        lines.append(f"- `{name}` ({scope}, type=`{worker_type}`): {description}{suffix}")
    return lines


def build_subagent_listing_section(
    *,
    agent_id: Any | None = None,
    tenant_id: Any | None = None,
    agent_data_dir: Path | str | None = None,
) -> str:
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
    lines.extend(
        _render_custom_definition_rows(
            agent_id=agent_id,
            tenant_id=tenant_id,
            agent_data_dir=agent_data_dir,
        )
    )
    return "\n".join(lines)
