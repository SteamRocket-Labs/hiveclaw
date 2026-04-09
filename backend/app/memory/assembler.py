"""Assemble retrieved memory items into a prompt-ready text section.

Groups items by kind in priority order, deduplicates by content hash,
and trims output to a character budget.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.memory.types import MemoryItem, MemoryKind, parse_utc_timestamp

# Memories older than this threshold get a freshness warning appended.
# L-03: Increased from 1 to 7 days — 1 day was too aggressive for agents running periodically
_FRESHNESS_WARNING_DAYS = 7

# Display order and section headers for each memory kind.
_SECTION_ORDER: list[tuple[MemoryKind, str]] = [
    (MemoryKind.WORKING, "[Working Memory]"),
    (MemoryKind.EPISODIC, "[Episodic Memory]"),
    (MemoryKind.SEMANTIC, "[Semantic Memory]"),
    (MemoryKind.EXTERNAL, "[External Memory]"),
]


def _content_hash(content: str) -> str:
    """Produce a short hash for deduplication."""
    return hashlib.md5(content.strip().lower().encode("utf-8")).hexdigest()  # noqa: S324


def _freshness_suffix(item: MemoryItem) -> str:
    """Return a freshness warning suffix for stale memories, empty for fresh ones."""
    ts_raw = item.metadata.get("timestamp")
    if not ts_raw:
        return ""
    ts = parse_utc_timestamp(ts_raw) if isinstance(ts_raw, str) else ts_raw
    if not isinstance(ts, datetime):
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age_days = (datetime.now(UTC) - ts).days
    if age_days > _FRESHNESS_WARNING_DAYS:
        return f" [{age_days}d ago — verify before acting]"
    return ""


class MemoryAssembler:
    """Assemble retrieved memory items into a prompt section."""

    def assemble(self, items: list[MemoryItem], budget_chars: int = 20000) -> str:
        """Render memory items grouped by kind, deduplicated and budget-trimmed.

        Returns a string with section headers ready to inject into a system prompt.
        """
        # Sort ALL items by score FIRST so dedup keeps highest-scored version (CR-01)
        sorted_items = sorted(items, key=lambda i: i.score, reverse=True)

        # Deduplicate by content hash (highest score wins since we sorted first)
        seen: set[str] = set()
        unique_items: list[MemoryItem] = []
        for item in sorted_items:
            h = _content_hash(item.content)
            if h not in seen:
                seen.add(h)
                unique_items.append(item)

        # Group by kind
        groups: dict[MemoryKind, list[MemoryItem]] = {}
        for item in unique_items:
            groups.setdefault(item.kind, []).append(item)
        # Items already sorted by score from the global sort above

        # Build output in priority order
        sections: list[str] = []
        total_chars = 0

        for kind, header in _SECTION_ORDER:
            kind_items = groups.get(kind)
            if not kind_items:
                continue

            lines: list[str] = [header]
            header_len = len(header) + 1
            for item in kind_items:
                freshness = _freshness_suffix(item) if kind != MemoryKind.WORKING else ""
                # B-06 fix: render category prefix for non-general types so LLM sees memory taxonomy
                _cat = item.metadata.get("category", "")
                _cat_prefix = f"[{_cat}] " if _cat and _cat != "general" and kind != MemoryKind.WORKING else ""
                line = f"- {_cat_prefix}{item.content}{freshness}" if kind != MemoryKind.WORKING else item.content
                line_len = len(line) + 1  # +1 for newline
                if total_chars + line_len > budget_chars:
                    break
                lines.append(line)
                total_chars += line_len

            # Only add the section if it has content beyond the header
            if len(lines) > 1:
                total_chars += header_len  # Count header only if section has content
                sections.append("\n".join(lines))

            if total_chars >= budget_chars:
                break

        return "\n\n".join(sections)
