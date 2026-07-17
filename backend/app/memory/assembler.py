"""Assemble retrieved memory items into a complete prompt-ready text section."""

from __future__ import annotations

from datetime import UTC, datetime

from app.memory.types import MemoryItem, MemoryKind, parse_utc_timestamp

# Memories older than this threshold get a freshness warning appended.
# L-03: Increased from 1 to 7 days — 1 day was too aggressive for agents running periodically
_FRESHNESS_WARNING_DAYS = 7
MAX_AUTOMATIC_MEMORY_ITEMS = 5
MAX_AUTOMATIC_ITEM_BYTES = 4096
MAX_AUTOMATIC_TURN_BYTES = MAX_AUTOMATIC_MEMORY_ITEMS * MAX_AUTOMATIC_ITEM_BYTES
MAX_AUTOMATIC_ITEM_LINES = 200
_MIN_USEFUL_SURFACE_BYTES = 256

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
        """Render model-selected items as bounded, recoverable excerpts.

        The model owns which items were selected.  This presentation layer only
        applies the CC automatic-surfacing contract: at most five selected
        items, at most 4 KiB / 200 lines per item, and an exact overall UTF-8
        byte budget.  Every excerpt carries a stable source/load reference, so
        the representation budget never becomes a semantic deletion.
        """
        if len(items) > MAX_AUTOMATIC_MEMORY_ITEMS:
            raise ValueError(f"automatic memory surfacing accepts at most {MAX_AUTOMATIC_MEMORY_ITEMS} items")
        budget_bytes = min(max(0, int(budget_chars)), MAX_AUTOMATIC_TURN_BYTES)
        if not items or budget_bytes < _MIN_USEFUL_SURFACE_BYTES:
            return ""

        # A single memory's complete rendered contribution, including the
        # section header and recovery ref, must remain inside 4 KiB.
        if len(items) == 1:
            budget_bytes = min(budget_bytes, MAX_AUTOMATIC_ITEM_BYTES)

        groups: dict[MemoryKind, list[MemoryItem]] = {}
        for item in items:
            groups.setdefault(item.kind, []).append(item)

        ordered_groups: list[tuple[str, list[MemoryItem]]] = []
        for kind, header in _SECTION_ORDER:
            kind_items = groups.get(kind)
            if kind_items:
                ordered_groups.append((header, kind_items))

        fixed_lines: list[str] = []
        for header, kind_items in ordered_groups:
            fixed_lines.append(header)
            for item in kind_items:
                fixed_lines.append(_reference_line(item))
        fixed_bytes = len("\n\n".join(fixed_lines).encode("utf-8"))
        if fixed_bytes > budget_bytes:
            return ""

        body_budget = budget_bytes - fixed_bytes
        remaining_items = len(items)
        sections: list[str] = []
        for header, kind_items in ordered_groups:
            lines: list[str] = [header]
            for item in kind_items:
                reference = _reference_line(item)
                fair_share = body_budget // max(1, remaining_items)
                per_item_overhead = len((reference + "\n").encode("utf-8"))
                excerpt_budget = max(0, min(fair_share, MAX_AUTOMATIC_ITEM_BYTES - per_item_overhead))
                excerpt = _render_excerpt(item, budget_bytes=excerpt_budget)
                if excerpt:
                    lines.append(excerpt)
                    body_budget -= len(excerpt.encode("utf-8"))
                lines.append(reference)
                remaining_items -= 1
            sections.append("\n".join(lines))

        rendered = "\n\n".join(sections)
        if len(rendered.encode("utf-8")) > budget_bytes:
            raise RuntimeError("memory_assembler_budget_invariant")
        return rendered


def _loadable_id(item: MemoryItem) -> str:
    for key in ("entry_id", "page_id"):
        value = str(item.metadata.get(key) or "").strip()
        if value:
            return value
    return ""


def _reference_line(item: MemoryItem) -> str:
    memory_id = _loadable_id(item)
    source = str(item.metadata.get("source_ref") or item.source or "memory").strip()
    if memory_id:
        return f'  [memory_ref id={memory_id} source={source}; load_memory(ids=["{memory_id}"])]'
    session_id = str(item.metadata.get("session_id") or "").strip()
    if session_id:
        return f'  [memory_ref session_id={session_id} source={source}; search_memory(scope="sessions")]'
    return f"  [memory_ref source={source}; search_memory(query=...) for full authorized evidence]"


def _render_excerpt(item: MemoryItem, *, budget_bytes: int) -> str:
    if budget_bytes <= 2:
        return ""
    freshness = _freshness_suffix(item)
    activation = _activation_suffix(item)
    category = str(item.metadata.get("category") or "").strip()
    prefix = f"- [{category}] " if category and category != "general" else "- "
    suffix = f"{activation}{freshness}"
    framing_bytes = len((prefix + suffix).encode("utf-8"))
    available = budget_bytes - framing_bytes
    if available <= 0:
        return ""
    excerpt = _clip_utf8_lines(
        str(item.content or ""),
        max_bytes=available,
        max_lines=max(1, MAX_AUTOMATIC_ITEM_LINES - 2),
    )
    if not excerpt:
        return ""
    return f"{prefix}{excerpt}{suffix}"


def _clip_utf8_lines(text: str, *, max_bytes: int, max_lines: int) -> str:
    if max_bytes <= 0 or max_lines <= 0:
        return ""
    original_lines = str(text or "").splitlines()
    candidate = "\n".join(original_lines[:max_lines])
    encoded = candidate.encode("utf-8")
    if len(encoded) <= max_bytes:
        return candidate
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
