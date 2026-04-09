"""§ Active Packs section — capability packs currently active in session."""

from __future__ import annotations

from typing import Any


def build_active_packs_section(
    active_packs: list[dict[str, Any]],
    *,
    budget_chars: int = 2000,
) -> str:
    """Build the active capability packs section.

    Args:
        active_packs: List of pack dicts with keys: name, summary, tools.
        budget_chars: Max chars for the packs section.
    """
    if not active_packs:
        return ""

    lines = [
        "## Active Capability Packs",
        "These capability packs are already active for the current invocation. Use them directly when relevant.",
        "",
    ]
    for pack in active_packs:
        tools = ", ".join(pack.get("tools", []))
        summary = pack.get("summary", "")
        lines.append(f"- {pack.get('name', 'unknown_pack')}: {summary}")
        if tools:
            lines.append(f"  Tools: {tools}")

    text = "\n".join(lines)
    if len(text) > budget_chars:
        text = text[:budget_chars] + "\n..."
    return text
