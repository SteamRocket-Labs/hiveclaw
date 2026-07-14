"""Assemble retrieved memory items into a complete prompt-ready text section."""

from __future__ import annotations

from datetime import UTC, datetime

from app.memory.types import MemoryItem, MemoryKind, parse_utc_timestamp

# Memories older than this threshold get a freshness warning appended.
# L-03: Increased from 1 to 7 days — 1 day was too aggressive for agents running periodically
_FRESHNESS_WARNING_DAYS = 7

# Display order and section headers for each memory kind.
_SECTION_ORDER: list[tuple[MemoryKind, str]] = [
    (MemoryKind.EPISODIC, "[Episodic Memory]"),
    (MemoryKind.SEMANTIC, "[Semantic Memory]"),
    (MemoryKind.EXTERNAL, "[External Memory]"),
]


def _freshness_suffix(item: MemoryItem) -> str:
    """Return a human-readable age suffix, with a warning for stale memories."""
    ts_raw = item.metadata.get("timestamp")
    if not ts_raw:
        return ""
    ts = parse_utc_timestamp(ts_raw) if isinstance(ts_raw, str) else ts_raw
    if not isinstance(ts, datetime):
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age_days = max(0, (datetime.now(UTC) - ts).days)
    if age_days > _FRESHNESS_WARNING_DAYS:
        return f" [{age_days}d ago — verify before acting]"
    return f" [{age_days}d ago]"


def _activation_suffix(item: MemoryItem) -> str:
    reasons = item.metadata.get("activation_reasons")
    if not isinstance(reasons, list) or not reasons:
        return ""
    compact = [str(reason).strip() for reason in reasons if str(reason).strip()]
    if not compact:
        return ""
    return f" [why={','.join(compact)}]"


class MemoryAssembler:
    """Assemble retrieved memory items into a prompt section."""

    def assemble(self, items: list[MemoryItem], budget_chars: int = 20000) -> str:
        """Render the authorized, model-selected items without another choice.

        Returns a string with section headers ready to inject into a system prompt.
        """
        del budget_chars  # Compatibility-only: final provider capacity gate owns prompt size.
        # Group by kind
        groups: dict[MemoryKind, list[MemoryItem]] = {}
        for item in items:
            groups.setdefault(item.kind, []).append(item)
        # Preserve the selector's order within each presentation section.

        # Build output in priority order
        sections: list[str] = []
        for kind, header in _SECTION_ORDER:
            kind_items = groups.get(kind)
            if not kind_items:
                continue

            lines: list[str] = [header]
            for item in kind_items:
                freshness = _freshness_suffix(item)
                activation = _activation_suffix(item)
                # B-06 fix: render category prefix for non-general types so LLM sees memory taxonomy
                _cat = item.metadata.get("category", "")
                _cat_prefix = f"[{_cat}] " if _cat and _cat != "general" else ""
                line = f"- {_cat_prefix}{item.content}{activation}{freshness}"
                lines.append(line)

            # Only add the section if it has content beyond the header
            if len(lines) > 1:
                sections.append("\n".join(lines))

        return "\n\n".join(sections)
