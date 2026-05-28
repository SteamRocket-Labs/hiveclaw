"""Durable queue for failed/in-flight T0→T2 extractions (P0-2a).

The hot path (`extract_agent.schedule_extract` invoked from
RESPONSE_COMPLETE) is fire-and-forget: if the underlying task fails (LLM
error, OOM, drain timeout, process crash) the message batch is silently
lost. Pattern fallback inside `extract_agent.extract` only mitigates LLM
failures — it does not cover the cases where the task itself never
finishes or the process dies before draining.

This module provides a tiny on-disk queue:
  - `enqueue(payload)`: persist payload before extraction begins.
  - `mark_done(entry_id)`: drop the entry once extraction finished cleanly.
  - `list_pending(...)`: iterate entries that didn't reach mark_done
    (used by P0-2b startup replay).
  - `purge_older_than(...)`: housekeeping for entries that already
    failed replay or are too stale to retry.

Filesystem layout (rooted at ``{AGENT_DATA_DIR}/.failed_extractions``):
  - One JSON file per scheduled extraction, named
    ``{agent_id}-{epoch_ms}-{uuid8}.json``
  - Successful extractions delete their file. Failed extractions remain
    until P0-2b explicitly retries them.

The queue is intentionally process-local + durable (filesystem). Multi
worker deployments share the same volume, so each worker can replay its
own pending work. Cross-worker idempotency is left to the extractor's
own cursor logic (cf. `extract_agent._cursors` / `learnings/.backfill_cursor.json`).
"""

from __future__ import annotations

import json
import logging
import hashlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.config import get_settings

logger = logging.getLogger(__name__)


_QUEUE_SUBDIR = ".failed_extractions"


def _queue_root() -> Path:
    """Root directory for queued extractions. Created lazily."""
    root = Path(get_settings().AGENT_DATA_DIR) / _QUEUE_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass(slots=True, frozen=True)
class ExtractionEntry:
    """A persisted extraction payload waiting to complete or be replayed."""

    entry_id: str  # filename stem; stable handle for mark_done / drop
    path: Path
    agent_id: str
    messages: list[dict]
    source: str
    tenant_id: str | None
    agent_name: str
    scheduled_at_ms: int


def _serialize(payload: dict[str, Any], path: Path) -> None:
    """Atomic-ish write: temp file + rename to avoid partial reads."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def derive_idempotency_key(messages: list[dict] | None, source: str) -> str | None:
    """Return a stable key when messages carry durable message/tool IDs.

    Falls back to ``None`` for anonymous in-memory transcripts so legacy callers
    keep the previous one-file-per-schedule behavior.
    """
    parts: list[str] = []
    for index, msg in enumerate(messages or []):
        stable_id = msg.get("id") or msg.get("message_id") or msg.get("tool_call_id")
        if stable_id:
            parts.append(f"{index}:{msg.get('role', '')}:{stable_id}")
        for tool_call in msg.get("tool_calls") or []:
            tool_id = tool_call.get("id")
            if tool_id:
                parts.append(f"{index}:tool_call:{tool_id}")
    if not parts:
        return None
    digest = hashlib.sha256((source + "\0" + "\0".join(parts)).encode("utf-8")).hexdigest()[:20]
    return digest


def enqueue(
    *,
    agent_id: uuid.UUID,
    messages: list[dict] | None,
    source: str,
    tenant_id: uuid.UUID | None,
    agent_name: str,
) -> str:
    """Persist payload before scheduling extraction. Returns entry_id.

    Caller must invoke `mark_done(entry_id)` after successful extraction
    or leave the entry in place so P0-2b can retry it.

    Raises OSError if persistence fails — caller should treat that as
    an unrecoverable scheduling failure (do not also call schedule_extract,
    or the message batch could double-process on later replay).
    """
    epoch_ms = int(time.time() * 1000)
    idempotency_key = derive_idempotency_key(messages, source)
    entry_id = f"{agent_id}-{idempotency_key}" if idempotency_key else f"{agent_id}-{epoch_ms}-{uuid.uuid4().hex[:8]}"
    path = _queue_root() / f"{entry_id}.json"
    if idempotency_key and path.exists():
        logger.debug("[ExtractQueue] Reusing idempotent entry %s (source=%s)", entry_id, source)
        return entry_id

    payload: dict[str, Any] = {
        "entry_id": entry_id,
        "idempotency_key": idempotency_key,
        "agent_id": str(agent_id),
        "messages": list(messages or []),
        "source": source,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "agent_name": agent_name,
        "scheduled_at_ms": epoch_ms,
    }
    _serialize(payload, path)
    logger.debug("[ExtractQueue] Enqueued %s (msgs=%d, source=%s)", entry_id, len(payload["messages"]), source)
    return entry_id


def mark_done(entry_id: str) -> None:
    """Remove a persisted entry once its extraction has completed cleanly.

    Idempotent — missing file is logged at debug, never raises.
    """
    path = _queue_root() / f"{entry_id}.json"
    try:
        path.unlink()
        logger.debug("[ExtractQueue] Marked done %s", entry_id)
    except FileNotFoundError:
        # Already cleared (concurrent worker / startup replay) — fine.
        logger.debug("[ExtractQueue] mark_done: entry %s not found (already cleared)", entry_id)


def list_pending(*, max_age_seconds: int | None = None) -> Iterable[ExtractionEntry]:
    """Yield entries waiting to be retried.

    `max_age_seconds`: optional upper bound; older entries are skipped
    (they remain on disk for operator inspection — use purge_older_than
    to delete). Default `None` means yield everything.
    """
    root = _queue_root()
    cutoff_ms: int | None = None
    if max_age_seconds is not None:
        cutoff_ms = int((time.time() - max_age_seconds) * 1000)

    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("[ExtractQueue] Skipping unreadable entry %s: %s", path.name, exc)
            continue

        scheduled_ms = int(payload.get("scheduled_at_ms", 0))
        if cutoff_ms is not None and scheduled_ms < cutoff_ms:
            continue

        yield ExtractionEntry(
            entry_id=payload["entry_id"],
            path=path,
            agent_id=payload["agent_id"],
            messages=list(payload.get("messages") or []),
            source=payload.get("source", "unknown"),
            tenant_id=payload.get("tenant_id"),
            agent_name=payload.get("agent_name", "Agent"),
            scheduled_at_ms=scheduled_ms,
        )


def purge_older_than(seconds: int) -> int:
    """Delete entries older than `seconds`. Returns count purged.

    Intended for operators: keeps the queue from growing without bound
    when many extractions have permanently failed and are not worth
    further retry.
    """
    root = _queue_root()
    cutoff_ms = int((time.time() - seconds) * 1000)
    purged = 0
    for path in root.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Unreadable — purge regardless to keep the queue sweepable.
            try:
                path.unlink()
                purged += 1
            except OSError as exc:
                logger.warning("[ExtractQueue] Failed to purge unreadable %s: %s", path.name, exc)
            continue
        if int(payload.get("scheduled_at_ms", 0)) < cutoff_ms:
            try:
                path.unlink()
                purged += 1
            except OSError as exc:
                logger.warning("[ExtractQueue] Failed to purge stale %s: %s", path.name, exc)
    if purged:
        logger.info("[ExtractQueue] Purged %d entries older than %ds", purged, seconds)
    return purged
