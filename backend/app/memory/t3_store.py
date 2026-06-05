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

from app.memory.md_store import (
    MEMORY_DEDUP_THRESHOLD,
    append_t3_entry,
    find_similar_t3_entries,
    t3_spec_for_category,
)
from app.memory.types import CONTAINER_CANDIDATES, MEMORY_CATEGORIES
from app.memory.write_gate import prepare_memory_write

logger = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 2000


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
