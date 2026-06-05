"""§ Memory Navigation — heat-ordered T3 entry index with load instructions.

Spec §8: the entry manifest gets a runtime consumer in prompt assembly.
Rendered as its OWN section in the dynamic suffix — never appended into
soul.md (soul stays identity, not navigation). Supports progressive
disclosure: the agent sees id + summary + heat, then calls
`load_memory(ids=[...])` for full Markdown with source refs.
"""

from __future__ import annotations

import uuid
from pathlib import Path

_MAX_ROWS = 20
_PREVIEW_CHARS = 90


def build_memory_navigation_section(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    max_rows: int = _MAX_ROWS,
) -> str:
    """Render the heat-ordered memory navigation table. Empty string when bare."""
    from app.memory.md_store import build_t3_entry_manifest, compute_entry_heat

    try:
        manifest = build_t3_entry_manifest(data_root, agent_id)
    except OSError:
        return ""
    if not manifest:
        return ""

    rows: list[tuple[float, str, str]] = []
    for entry in manifest:
        heat = compute_entry_heat(entry.metadata)
        recall_count = entry.metadata.get("access_count", "0")
        last_recalled = entry.metadata.get("last_accessed", "never")
        if last_recalled not in ("", "never"):
            last_recalled = last_recalled[:10]
        preview = entry.preview[:_PREVIEW_CHARS]
        rows.append(
            (
                heat,
                entry.timestamp or "",
                f"| {entry.entry_id} | {entry.filename} | {entry.category} | {heat:g} | "
                f"{recall_count} | {last_recalled} | {preview} |",
            )
        )

    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    table = "\n".join(row for _heat, _ts, row in rows[:max_rows])
    omitted = max(0, len(rows) - max_rows)
    footer = f"\n({omitted} colder entries omitted — search_memory finds them)" if omitted else ""

    return (
        "## Memory Navigation\n"
        "Heat-ordered index of your long-term memory (recall telemetry: heat = recall_count + recency). "
        "Load full entries with `load_memory(ids=[...])` before relying on a preview.\n\n"
        "| id | file | category | heat | recall_count | last_recalled | preview |\n"
        "|----|------|----------|------|--------------|---------------|---------|\n"
        f"{table}{footer}"
    )
