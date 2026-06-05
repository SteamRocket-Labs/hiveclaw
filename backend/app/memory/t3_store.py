"""Governed T3 append API — the single write path for durable T3 memory.

Spec: docs/agent-memory-md-first-spec.md §12 P2. Every durable T3 write —
agent ``save_memory`` tool, heartbeat curation, dream, manual ops — flows
through :func:`append_t3_memory_candidate`:

    prepare_memory_write        (privacy / form / lifecycle metadata gate)
      -> find_similar_t3_entries (semantic near-dedup)
      -> append_t3_entry         (MD write + lifecycle record + index rebuild)
      -> sync_t3_to_hindsight    (derived read-side accelerator, best effort)

Raw ``write_file`` / ``edit_file`` access under ``memory/`` is refused at the
workspace tool layer, so no caller can bypass this gate.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.memory.lifecycle_store import (
    LifecycleStatus,
    MemoryLifecycleStore,
    lifecycle_path,
)
from app.memory.md_store import (
    MEMORY_DEDUP_THRESHOLD,
    _stable_entry_id,
    append_t3_entry,
    find_similar_t3_entries,
    memory_dir,
    parse_entry_record,
    rebuild_index,
    t3_spec_for_category,
)
from app.memory.types import CONTAINER_CANDIDATES, MEMORY_CATEGORIES
from app.memory.write_gate import prepare_memory_write

logger = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 2000

# Retirement reasons that record a SUPERSEDED edge; everything else archives.
_SUPERSEDE_REASONS = frozenset({"superseded", "dedup_superseded", "contradiction_resolved"})


@dataclass(slots=True)
class T3AppendResult:
    """Outcome of a governed T3 append attempt."""

    status: str  # accepted | rejected | duplicate
    category: str
    entry_id: str = ""
    path: str = ""
    reason: str = ""
    sensitivity: str = ""
    similar: dict | None = None


async def append_t3_memory_candidate(
    agent_id: uuid.UUID,
    *,
    category: str,
    content: str,
    source_refs: list[str] | tuple[str, ...] | str | None = None,
    evidence: str = "",
    confidence: float | None = None,
    proposed_by: str = "manual",
    container_candidate: str | None = None,
    tenant_id: uuid.UUID | None = None,
    data_root: Path | None = None,
) -> T3AppendResult:
    """Append one memory candidate to T3 through the full write gate.

    ``proposed_by`` identifies the distiller lane (``extractor`` /
    ``heartbeat`` / ``dream`` / ``agent_tool`` / ``manual``) and is stamped
    into entry metadata for audit. Returns a structured result; never raises
    on gate rejection or duplicates — those are decisions, not errors.
    """
    if data_root is None:
        from app.config import get_settings

        data_root = Path(get_settings().AGENT_DATA_DIR)

    normalized_category = (category or "general").strip().lower()
    if normalized_category not in MEMORY_CATEGORIES:
        normalized_category = "general"

    trimmed = (content or "").strip()[:_MAX_CONTENT_CHARS]
    if not trimmed:
        return T3AppendResult(status="rejected", category=normalized_category, reason="empty content")

    # 1. Write gate: privacy classification (PL4 rejection), form lint,
    #    lifecycle metadata (entry_id, sensitivity, status, version).
    decision = prepare_memory_write(trimmed, category=normalized_category, evidence_refs=source_refs)
    if decision.rejected:
        logger.info(
            "[T3Store] write rejected for agent=%s category=%s by=%s: %s",
            agent_id,
            normalized_category,
            proposed_by,
            decision.reason,
        )
        return T3AppendResult(
            status="rejected",
            category=decision.category,
            reason=decision.reason,
            sensitivity=decision.sensitivity,
        )

    # 2. Semantic near-dedup against the target T3 file.
    similar = find_similar_t3_entries(
        data_root,
        agent_id,
        content=decision.content,
        category=decision.category,
        threshold=MEMORY_DEDUP_THRESHOLD,
        limit=1,
    )
    if similar:
        return T3AppendResult(
            status="duplicate",
            category=decision.category,
            reason=f"similar entry exists (similarity={similar[0]['similarity']:.2f})",
            sensitivity=decision.sensitivity,
            similar=similar[0],
        )

    # 3. Stamp lane + routing metadata, then append (append_t3_entry records
    #    the lifecycle entry and rebuilds INDEX.md).
    metadata = dict(decision.metadata)
    metadata["proposed_by"] = (proposed_by or "manual").strip().lower() or "manual"
    normalized_container = (container_candidate or "").strip().lower()
    if normalized_container in CONTAINER_CANDIDATES:
        metadata["container"] = normalized_container
    if evidence.strip():
        metadata.setdefault("ev", evidence.strip().lower())
    if confidence is not None:
        metadata.setdefault("conf", f"{max(0.0, min(1.0, float(confidence))):.2f}")

    path = append_t3_entry(
        data_root,
        agent_id,
        category=decision.category,
        content=decision.content,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        metadata=metadata,
    )

    # 4. Derived read-side accelerator — best effort, never blocks the write.
    try:
        from app.memory import hindsight_sync

        await hindsight_sync.sync_t3_to_hindsight(agent_id, tenant_id, data_root=data_root)
    except Exception as exc:  # noqa: BLE001 — derived index failure must not fail durable truth
        logger.warning("[T3Store] hindsight sync failed (non-fatal) for %s: %s", agent_id, exc)

    spec = t3_spec_for_category(decision.category)
    return T3AppendResult(
        status="accepted",
        category=decision.category,
        entry_id=metadata.get("entry_id", ""),
        path=str(path),
        reason=f"appended to memory/{spec['filename']} by {metadata['proposed_by']}",
        sensitivity=decision.sensitivity,
    )


# ── Reversible retirement (spec §4.9 / §12 P3) ──
#
# Retirement de-indexes from active recall and archives in Markdown; it never
# physically deletes evidence. Active T3 files stay clean so every reader
# (retriever, manifest, prompt injection, hindsight) naturally sees only
# active entries; memory/archive.md plus lifecycle.json hold the audit trail.


def archive_t3_lines(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    filename: str,
    lines: list[str],
    reason: str,
    superseded_by: str = "",
) -> int:
    """Archive already-removed T3 lines into memory/archive.md + lifecycle.

    Pure archival: callers own the active-file rewrite. Each line gets an
    archive row (original date preserved) and a lifecycle retirement edge —
    SUPERSEDED for merge/contradiction reasons, ARCHIVED for decay/cap.
    Returns the number of lines archived.
    """
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if not cleaned:
        return 0

    mem_dir = memory_dir(data_root, agent_id)
    mem_dir.mkdir(parents=True, exist_ok=True)
    archive_path = mem_dir / "archive.md"
    existing = (
        archive_path.read_text(encoding="utf-8", errors="replace")
        if archive_path.exists()
        else "# Memory Archive\n\nRetired entries — de-indexed from active recall, preserved as evidence.\n\n"
    )

    normalized_reason = (reason or "archived").strip().lower() or "archived"
    status = LifecycleStatus.SUPERSEDED if normalized_reason in _SUPERSEDE_REASONS else LifecycleStatus.ARCHIVED
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    store = MemoryLifecycleStore(lifecycle_path(data_root, agent_id))

    rows: list[str] = []
    for line in cleaned:
        record = parse_entry_record(line)
        if not record.content:
            continue
        entry_id = record.metadata.get("entry_id") or _stable_entry_id(filename, record.content)
        meta_parts = [f"[from={filename}]", f"[reason={normalized_reason}]", f"[entry_id={entry_id}]"]
        if record.timestamp:
            meta_parts.append(f"[orig_date={record.timestamp}]")
        if superseded_by:
            meta_parts.append(f"[superseded_by={_sanitize_archive_meta(superseded_by)}]")
        rows.append(f"- [{today}]{''.join(meta_parts)} {record.content}")

        store.mark_retired(
            entry_id,
            status=status,
            content=record.content,
            superseded_by=_sanitize_archive_meta(superseded_by) if superseded_by else None,
            metadata={"from": filename, "reason": normalized_reason, "retired_at": today},
        )

    if not rows:
        return 0

    updated = existing.rstrip() + "\n" + "\n".join(rows) + "\n"
    archive_path.write_text(updated, encoding="utf-8")
    return len(rows)


def retire_t3_entries(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    filename: str,
    drops: list[str],
    reason: str,
    superseded_by: str = "",
) -> int:
    """Remove matching entry lines from an active T3 file and archive them.

    ``drops`` use substring matching against entry lines (the dream decision
    contract). Returns the number of entries retired. Rebuilds INDEX.md when
    anything moved so de-indexing is immediate.
    """
    needles = [str(d).strip() for d in drops if str(d).strip()]
    if not needles:
        return 0

    path = memory_dir(data_root, agent_id) / filename
    if not path.exists():
        return 0

    # The canonical (kept) line often contains a drop needle as a substring
    # — e.g. keep "...emoji in responses (3rd confirmation)" vs drop
    # "...emoji in responses". Never retire the line the merge keeps.
    keep_marker = parse_entry_record(superseded_by).content if superseded_by.strip() else ""

    content = path.read_text(encoding="utf-8", errors="replace")
    kept_lines: list[str] = []
    dropped_lines: list[str] = []
    for line in content.splitlines():
        is_entry = line.strip().startswith("-")
        if is_entry and keep_marker and keep_marker in line:
            kept_lines.append(line)
            continue
        if is_entry and any(needle in line for needle in needles):
            dropped_lines.append(line)
            continue
        kept_lines.append(line)

    if not dropped_lines:
        return 0

    path.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
    archived = archive_t3_lines(
        data_root,
        agent_id,
        filename=filename,
        lines=dropped_lines,
        reason=reason,
        superseded_by=superseded_by,
    )
    rebuild_index(data_root, agent_id)
    return archived if archived else len(dropped_lines)


def _sanitize_archive_meta(value: str) -> str:
    return " ".join(str(value).replace("[", "(").replace("]", ")").split())[:160]
