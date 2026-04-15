"""Helpers to migrate legacy memory files into the MD-first layout."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.memory.md_store import append_t3_entry, ensure_t3_layout
from app.memory.t2_store import append_t2_entries, ensure_t2_layout

logger = logging.getLogger(__name__)

_MARKER_FILENAME = ".legacy_migrated"

_LEGACY_T2_FILES: dict[str, str] = {
    "LEARNINGS.md": "general",
    "ERRORS.md": "error",
    "FEATURE_REQUESTS.md": "request",
}
_SKIP_LINES = (
    "record operation failures here for review during heartbeat.",
    "record corrections, insights, and best practices here.",
    "record important information and knowledge here.",
    "capability gaps and user wishes.",
    "user corrections, preferences, and agent discoveries.",
    "execution failures and blocked approaches.",
)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _should_skip(text: str) -> bool:
    normalized = _normalize(text).lower()
    if not normalized:
        return True
    if normalized in {"# memory", "# learnings", "# errors", "# feature requests", "# old memory"}:
        return True
    return any(fragment in normalized for fragment in _SKIP_LINES)


def _extract_legacy_entries(content: str) -> list[str]:
    entries: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        text = _normalize(" ".join(paragraph))
        paragraph.clear()
        if text and not _should_skip(text):
            entries.append(text)

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        if line.startswith("#") or line == "---":
            flush_paragraph()
            continue
        if line.startswith("- "):
            flush_paragraph()
            text = _normalize(line[2:])
            if text and not _should_skip(text):
                entries.append(text)
            continue
        paragraph.append(line)

    flush_paragraph()

    deduped: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        key = entry.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def migrate_legacy_memory_tree(data_root: Path, agent_id: uuid.UUID) -> dict[str, int]:
    """Migrate legacy memory files into canonical T2/T3 files.

    Idempotent fast-path: if `.legacy_migrated` marker exists, skip immediately.
    Marker is written after first successful run so subsequent calls are O(1).
    """
    root = Path(data_root) / str(agent_id)
    marker_path = root / _MARKER_FILENAME
    if marker_path.exists():
        return {
            "migrated_t2_entries": 0,
            "migrated_t3_entries": 0,
            "removed_legacy_files": 0,
            "skipped": True,
        }

    memory_dir = root / "memory"
    learnings_dir = memory_dir / "learnings"
    legacy_t2_entries: list[dict[str, str]] = []
    legacy_t3_entries: list[str] = []
    removed_legacy_files = 0

    # Read and remove legacy files first. On case-insensitive filesystems, ERRORS.md
    # and errors.md refer to the same inode, so canonical files must be written only
    # after legacy names are gone.
    for filename, category in _LEGACY_T2_FILES.items():
        path = learnings_dir / filename
        if not path.exists():
            continue
        entries = _extract_legacy_entries(path.read_text(encoding="utf-8", errors="replace"))
        legacy_t2_entries.extend({"category": category, "content": entry} for entry in entries)
        path.unlink()
        removed_legacy_files += 1

    legacy_memory_paths = [root / "memory.md", memory_dir / "memory.md"]
    for path in legacy_memory_paths:
        if not path.exists():
            continue
        legacy_t3_entries.extend(_extract_legacy_entries(path.read_text(encoding="utf-8", errors="replace")))
        path.unlink()
        removed_legacy_files += 1

    ensure_t3_layout(data_root, agent_id)
    ensure_t2_layout(data_root, agent_id)

    migrated_t2_entries = 0
    migrated_t3_entries = 0
    if legacy_t2_entries:
        migrated_t2_entries += append_t2_entries(
            data_root,
            agent_id,
            extractions=legacy_t2_entries,
            source="legacy_migration",
        )
    for entry in legacy_t3_entries:
        append_t3_entry(
            data_root,
            agent_id,
            category="general",
            content=entry,
        )
        migrated_t3_entries += 1

    root.mkdir(parents=True, exist_ok=True)
    marker_payload = (
        f"migrated_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"migrated_t2_entries: {migrated_t2_entries}\n"
        f"migrated_t3_entries: {migrated_t3_entries}\n"
        f"removed_legacy_files: {removed_legacy_files}\n"
    )
    try:
        with open(marker_path, "x", encoding="utf-8") as f:
            f.write(marker_payload)
    except FileExistsError:
        logger.debug(
            "[legacy_migration] marker already written for agent %s (concurrent migration won race)",
            agent_id,
        )

    return {
        "migrated_t2_entries": migrated_t2_entries,
        "migrated_t3_entries": migrated_t3_entries,
        "removed_legacy_files": removed_legacy_files,
        "skipped": False,
    }
