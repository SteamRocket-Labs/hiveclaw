"""Governed T3 append API — the single write path for durable T3 memory.

Spec: docs/agent-memory-md-first-spec.md §12 P2. Every durable T3 write —
agent ``save_memory`` tool, heartbeat curation, dream, manual ops — flows
through :func:`append_t3_memory_candidate`:

    prepare_memory_write        (privacy / form / lifecycle metadata gate)
      -> find_similar_t3_entries (semantic near-dedup)
      -> append_t3_entry         (MD write + lifecycle record + index rebuild)
      -> sync_t3_to_memory_enhancement (optional no-op adapter boundary)

Raw ``write_file`` / ``edit_file`` access under ``memory/`` is refused at the
workspace tool layer, so no caller can bypass this gate.
"""

from __future__ import annotations

import logging
import re
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
    T3_FILE_SPECS,
    _stable_entry_id,
    append_t3_entry,
    find_similar_t3_entries,
    memory_dir,
    parse_entry_record,
    rebuild_index,
    t3_spec_for_category,
)
from app.memory.types import CONTAINER_CANDIDATES, MEMORY_CATEGORIES
from app.memory.write_gate import prepare_memory_write_with_llm

logger = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 2000
_HARMFUL_EVIDENCE_MARKERS = frozenset(
    {
        "harmful",
        "misleading",
        "negative",
        "failure",
        "failed",
        "wrong",
        "rejected",
        "user_rejected",
    }
)

# Retirement reasons that record a SUPERSEDED edge; everything else archives.
_SUPERSEDE_REASONS = frozenset({"superseded", "dedup_superseded", "contradiction_resolved"})

# D5: episodic-observation lane gate (agent_tool lane only). Routine scan/poll
# logs ("14 expos in window, no change") are runtime evidence — the extractor
# routes these to its `artifact_only` lane via an LLM judge, but the agent_tool
# write path bypasses that judge, so episodic logs pile into durable strategies.md.
# This mechanical backstop refuses the clearest episodic logs. It deliberately
# requires BOTH a routine-observation verb AND a null/count observation so a real
# reusable strategy ("scan three times daily works best") is never a false
# positive — mechanical steps back off rather than corrupt (D9 lesson, AI-Native L1).
_EPISODIC_OBSERVATION_VERB = re.compile(r"(scan|poll|monitor|sweep|crawl|扫描|轮询|巡检|监控)", re.IGNORECASE)
_EPISODIC_NULL_OR_COUNT = re.compile(
    r"(no\s+change|unchanged|nothing\s+new|no\s+new\b|无变化|无更新|没有变化|没有更新|"
    r"\d+\s*(?:个|项|条|家|expos?|items?|results?|sites?)\b[^。.\n]{0,16}(?:无|没有|unchanged|no\s+change))",
    re.IGNORECASE,
)


def looks_episodic_observation(content: str) -> bool:
    """High-confidence episodic scan log: a routine-observation verb AND a
    null/count observation. Conservative by design — single signals pass."""
    text = (content or "").strip()
    if not text:
        return False
    return bool(_EPISODIC_OBSERVATION_VERB.search(text) and _EPISODIC_NULL_OR_COUNT.search(text))


def _safe_counter(value: str | None) -> int:
    try:
        return max(0, int(str(value or "0").strip()))
    except (TypeError, ValueError):
        return 0


def _reinforcement_kind(evidence: str) -> str:
    normalized = (evidence or "").strip().lower()
    if any(marker in normalized for marker in _HARMFUL_EVIDENCE_MARKERS):
        return "harmful"
    return "helpful"


def _memory_signature(*, category: str, content: str) -> str:
    spec = t3_spec_for_category(category)
    return _stable_entry_id(spec["filename"], content)


def _counter_seed_metadata(
    *,
    category: str,
    content: str,
    evidence: str,
    proposed_by: str,
    now: str,
) -> dict[str, str]:
    kind = _reinforcement_kind(evidence)
    return {
        "memory_signature": _memory_signature(category=category, content=content),
        "reinforcement_count": "1",
        "helpful_count": "1" if kind == "helpful" else "0",
        "harmful_count": "1" if kind == "harmful" else "0",
        "last_reinforced_at": now,
        "last_reinforcement_evidence": (evidence or "").strip().lower() or "unspecified",
        "last_reinforced_by": (proposed_by or "manual").strip().lower() or "manual",
    }


def _increment_reinforcement_counters(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    entry_id: str,
    content: str,
    category: str,
    evidence: str,
    proposed_by: str,
) -> dict[str, str]:
    store = MemoryLifecycleStore(lifecycle_path(data_root, agent_id))
    existing = store.metadata_map().get(entry_id, {})
    kind = _reinforcement_kind(evidence)
    now = datetime.now(timezone.utc).isoformat()
    reinforcement_count = _safe_counter(existing.get("reinforcement_count")) + 1
    helpful_count = _safe_counter(existing.get("helpful_count")) + (1 if kind == "helpful" else 0)
    harmful_count = _safe_counter(existing.get("harmful_count")) + (1 if kind == "harmful" else 0)
    updates = {
        "memory_signature": existing.get("memory_signature") or _memory_signature(category=category, content=content),
        "reinforcement_count": str(reinforcement_count),
        "helpful_count": str(helpful_count),
        "harmful_count": str(harmful_count),
        "last_reinforced_at": now,
        "last_reinforcement_evidence": (evidence or "").strip().lower() or "unspecified",
        "last_reinforced_by": (proposed_by or "manual").strip().lower() or "manual",
    }
    store.upsert_active(entry_id, content=content, metadata=updates)
    rebuild_index(data_root, agent_id)
    return updates


@dataclass(slots=True)
class T3AppendResult:
    """Outcome of a governed T3 append attempt."""

    status: str  # accepted | rejected | duplicate | episodic
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
    parent_id: str | None = None,
    supersedes: list[str] | tuple[str, ...] | str | None = None,
    superseded_by: str | None = None,
    dedup_exclude_entry_ids: list[str] | tuple[str, ...] | set[str] | None = None,
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

    # 1. Write gate: LLM-primary threat classification, privacy classification
    #    (PL4 rejection), form lint, lifecycle metadata (entry_id,
    #    sensitivity, status, version). If no tenant/model context is available,
    #    the gate records a regex_fallback decision in metadata.
    decision = await prepare_memory_write_with_llm(
        trimmed,
        category=normalized_category,
        evidence_refs=source_refs,
        parent_id=parent_id,
        supersedes=supersedes,
        superseded_by=superseded_by,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )
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

    # 1b. D5 lane gate (agent_tool only): episodic scan/observation logs are
    #     runtime evidence, not durable memory. extractor/heartbeat/dream are
    #     exempt — they ran the LLM lane judge upstream.
    if (proposed_by or "").strip().lower() == "agent_tool" and looks_episodic_observation(decision.content):
        logger.info("[T3Store] episodic write refused for agent=%s: %r", agent_id, decision.content[:80])
        return T3AppendResult(
            status="episodic",
            category=decision.category,
            reason=(
                "This reads as an episodic observation (routine scan / 'no change' log). "
                "It belongs in your session log (workspace/T0), not durable memory. "
                "If there's a reusable rule behind it, save that instead "
                "(e.g. 'this scan cadence catches changes fastest')."
            ),
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
    excluded_ids = {str(entry_id).strip() for entry_id in (dedup_exclude_entry_ids or []) if str(entry_id).strip()}
    if excluded_ids:
        similar = [hit for hit in similar if str(hit.get("id") or "").strip() not in excluded_ids]
    if similar:
        reinforced = dict(similar[0])
        counter_delta = _increment_reinforcement_counters(
            data_root,
            agent_id,
            entry_id=str(reinforced["id"]),
            content=str(reinforced.get("content") or decision.content),
            category=decision.category,
            evidence=evidence,
            proposed_by=proposed_by,
        )
        reinforced["counter_delta"] = counter_delta
        return T3AppendResult(
            status="duplicate",
            category=decision.category,
            entry_id=str(reinforced["id"]),
            reason=f"similar entry reinforced (similarity={reinforced['similarity']:.2f})",
            sensitivity=decision.sensitivity,
            similar=reinforced,
        )

    # 3. Stamp lane + routing metadata, then append (append_t3_entry records
    #    the lifecycle entry and rebuilds INDEX.md).
    metadata = dict(decision.metadata)
    now_iso = datetime.now(timezone.utc).isoformat()
    metadata["proposed_by"] = (proposed_by or "manual").strip().lower() or "manual"
    metadata.update(
        {
            key: value
            for key, value in _counter_seed_metadata(
                category=decision.category,
                content=decision.content,
                evidence=evidence,
                proposed_by=proposed_by,
                now=now_iso,
            ).items()
            if key not in metadata
        }
    )
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

    # 4. Optional enhancement adapter boundary — best effort, never blocks the write.
    try:
        from app.memory.enhancement import sync_t3_to_memory_enhancement

        await sync_t3_to_memory_enhancement(agent_id, tenant_id, data_root=data_root)
    except Exception as exc:  # noqa: BLE001 - optional enhancement must not fail durable truth
        logger.warning("[T3Store] memory enhancement sync failed (non-fatal) for %s: %s", agent_id, exc)

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
# (retriever, manifest, prompt injection) naturally sees only
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


def _archive_backfill_originals(data_root: Path, agent_id: uuid.UUID, originals: list[str]) -> None:
    """Append pre-backfill dirty lines to archive.md as reversible evidence.

    Unlike :func:`archive_t3_lines` this does NOT retire the entries — they stay
    active in clean prose; only the original inline-metadata text is preserved so
    the D2 strip is auditable and reversible.
    """
    if not originals:
        return
    mem_dir = memory_dir(data_root, agent_id)
    mem_dir.mkdir(parents=True, exist_ok=True)
    archive_path = mem_dir / "archive.md"
    existing = (
        archive_path.read_text(encoding="utf-8", errors="replace")
        if archive_path.exists()
        else "# Memory Archive\n\nRetired entries — de-indexed from active recall, preserved as evidence.\n\n"
    )
    if "## D2 Prose Backfill" not in existing:
        existing = (
            existing.rstrip()
            + "\n\n## D2 Prose Backfill\n\n"
            + "Original inline-metadata lines (entries stay ACTIVE in clean prose; kept as reversible evidence).\n"
        )
    archive_path.write_text(existing.rstrip() + "\n" + "\n".join(originals) + "\n", encoding="utf-8")


def backfill_t3_prose(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    dry_run: bool = True,
) -> dict:
    """D2: rewrite existing T3 ``.md`` lines to clean ``[date][entry_id] content``.

    Every other inline field (sensitivity/status/version/refs + D1 telemetry) is
    migrated into the lifecycle sidecar FIRST — so access control never loses a
    PL2/PL3 marker — then the original dirty line is archived (reversible) and
    the prose rewritten. ``dry_run=True`` reports the diff without touching disk.
    Already-clean ``[date][entry_id]`` lines are a no-op (idempotent).
    """
    from app.memory.md_store import T3_FILE_SPECS

    report: dict = {"dry_run": dry_run, "files_changed": 0, "entries_migrated": 0, "diff": []}
    mem_dir = memory_dir(data_root, agent_id)
    if not mem_dir.exists():
        return report

    store = MemoryLifecycleStore(lifecycle_path(data_root, agent_id))
    originals: list[str] = []

    for spec in T3_FILE_SPECS:
        path = mem_dir / spec["filename"]
        if not path.exists():
            continue
        out_lines: list[str] = []
        file_changed = False
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.rstrip()
            if not line.startswith("- ["):
                out_lines.append(raw)
                continue
            record = parse_entry_record(line)
            if not record.content:
                out_lines.append(raw)
                continue
            extra = {key: value for key, value in record.metadata.items() if key != "entry_id"}
            if not extra and record.metadata.get("entry_id"):
                out_lines.append(raw)  # already clean prose — no-op
                continue
            entry_id = record.metadata.get("entry_id") or _stable_entry_id(spec["filename"], record.content)
            report["entries_migrated"] += 1
            report["diff"].append(f"{spec['filename']}: [{entry_id}] strip {sorted(extra)}")
            date = record.timestamp or ""
            clean = (
                f"- [{date}][entry_id={entry_id}] {record.content}"
                if date
                else f"- [entry_id={entry_id}] {record.content}"
            )
            out_lines.append(clean)
            file_changed = True
            if not dry_run:
                # Migrate the full inline metadata into the sidecar BEFORE the
                # strip lands — sensitivity must survive so access control holds.
                store.upsert_active(entry_id, content=record.content, metadata=record.metadata)
                originals.append(line)
        if file_changed:
            report["files_changed"] += 1
            if not dry_run:
                path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    if not dry_run and report["entries_migrated"]:
        _archive_backfill_originals(data_root, agent_id, originals)
        rebuild_index(data_root, agent_id)
    return report


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

    # Guard the consolidated rule: the dream prompt teaches a SYNTHESIZED keep
    # line (e.g. "...emoji in responses (3rd confirmation)") that may not exist
    # verbatim in the file. If no kept line already carries it, append it —
    # otherwise dropping every variant erases the merged rule entirely and the
    # supersession edge points at a line that was never written.
    if keep_marker and not any(keep_marker in ln for ln in kept_lines):
        keep_line = superseded_by.strip()
        kept_lines.append(keep_line if keep_line.startswith("-") else f"- {keep_line}")

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


def retire_t3_entries_by_id(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    entry_ids: list[str] | tuple[str, ...] | set[str],
    reason: str,
    superseded_by: str = "",
) -> int:
    """Remove active T3 entries by stable entry_id and archive evidence.

    This is the tool/API retirement path: unlike dream consolidation's
    substring-based ``retire_t3_entries``, it only matches exact entry ids and
    never synthesizes a kept line. The old prose leaves active recall
    immediately; ``archive.md`` + ``lifecycle.json`` keep the reversible audit
    trail.
    """
    targets = {str(entry_id).strip() for entry_id in entry_ids if str(entry_id).strip()}
    if not targets:
        return 0

    mem_dir = memory_dir(data_root, agent_id)
    if not mem_dir.exists():
        return 0

    total_archived = 0
    for spec in T3_FILE_SPECS:
        path = mem_dir / spec["filename"]
        if not path.exists():
            continue
        kept_lines: list[str] = []
        dropped_lines: list[str] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            record = parse_entry_record(line)
            if not line.strip().startswith("-") or not record.content:
                kept_lines.append(line)
                continue
            entry_id = record.metadata.get("entry_id") or _stable_entry_id(spec["filename"], record.content)
            if entry_id in targets:
                dropped_lines.append(line)
                continue
            kept_lines.append(line)

        if not dropped_lines:
            continue

        path.write_text("\n".join(kept_lines).rstrip() + "\n", encoding="utf-8")
        archived = archive_t3_lines(
            data_root,
            agent_id,
            filename=spec["filename"],
            lines=dropped_lines,
            reason=reason,
            superseded_by=superseded_by,
        )
        total_archived += archived if archived else len(dropped_lines)

    if total_archived:
        rebuild_index(data_root, agent_id)
    return total_archived


def _sanitize_archive_meta(value: str) -> str:
    return " ".join(str(value).replace("[", "(").replace("]", ")").split())[:160]
