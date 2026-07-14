"""§ Active Runtime Tool Groups section — deferred tool groups active in session."""

from __future__ import annotations

from typing import Any

_DEFAULT_BUDGET_CHARS = 1200


def _format_tools_inline(tools: list[str]) -> str:
    """Render the complete callable surface visible to the model."""
    return ", ".join(tools)


def _trim_summary(summary: str) -> str:
    """Normalize whitespace without discarding semantic content."""
    if not summary:
        return ""
    return " ".join(summary.split())


def build_active_tool_groups_section(
    active_tool_groups: list[dict[str, Any]],
    *,
    budget_chars: int = _DEFAULT_BUDGET_CHARS,
) -> str:
    """Build the active runtime tool groups section.

    Args:
        active_tool_groups: List of tool group dicts with keys: name, summary, tools.
        budget_chars: Compatibility-only advisory. Provider capacity is
            enforced after complete prompt assembly; this section never
            decides which callable tools the model is allowed to see.
    """
    del budget_chars
    if not active_tool_groups:
        return ""

    lines = [
        "## Active Runtime Tool Groups",
        "These runtime tool groups are already active for the current invocation. Use them directly when relevant.",
        "",
    ]
    for pack in active_tool_groups:
        name = pack.get("name", "unknown_pack")
        summary = _trim_summary(pack.get("summary", ""))
        tools = pack.get("tools", []) or []
        tools_inline = _format_tools_inline(tools)

        head = f"- {name}"
        if summary:
            head += f": {summary}"
        lines.append(head)
        if tools_inline:
            lines.append(f"  Tools: {tools_inline}")

    return "\n".join(lines)
