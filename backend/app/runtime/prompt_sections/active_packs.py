"""§ Active Packs section — capability packs currently active in session."""

from __future__ import annotations

from typing import Any

# P1-W2-6: tighten the per-pack prompt footprint so that adding a pack
# doesn't quietly inflate every round's input cost. Packs grow over time
# as new tools land — without these caps a single feishu pack at 30+
# tools could spend ~600 chars per pack on an enumerable list the model
# never needs verbatim.
_SUMMARY_MAX_CHARS = 100
_TOOLS_PREVIEW_COUNT = 5
_DEFAULT_BUDGET_CHARS = 1200


def _format_tools_inline(tools: list[str]) -> str:
    """Show first N tools and a count of the rest; never enumerate everything."""
    if not tools:
        return ""
    preview = tools[:_TOOLS_PREVIEW_COUNT]
    rendered = ", ".join(preview)
    remainder = len(tools) - len(preview)
    if remainder > 0:
        rendered += f" (+{remainder} more)"
    return rendered


def _trim_summary(summary: str) -> str:
    """Single-line summary capped at `_SUMMARY_MAX_CHARS`."""
    if not summary:
        return ""
    flat = " ".join(summary.split())  # collapse newlines/spaces
    if len(flat) <= _SUMMARY_MAX_CHARS:
        return flat
    return flat[: _SUMMARY_MAX_CHARS - 1].rstrip() + "…"


def build_active_packs_section(
    active_packs: list[dict[str, Any]],
    *,
    budget_chars: int = _DEFAULT_BUDGET_CHARS,
) -> str:
    """Build the active capability packs section.

    Args:
        active_packs: List of pack dicts with keys: name, summary, tools.
        budget_chars: Max chars for the packs section. Default 1200 (was
            2000) — packs are referential signposts, not full docs.
    """
    if not active_packs:
        return ""

    lines = [
        "## Active Capability Packs",
        "These capability packs are already active for the current invocation. "
        "Use them directly when relevant.",
        "",
    ]
    for pack in active_packs:
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

    text = "\n".join(lines)
    if len(text) > budget_chars:
        text = text[: budget_chars - 4].rstrip() + "\n..."
    return text
