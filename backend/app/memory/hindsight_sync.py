"""Incremental sync from T3 markdown to Hindsight bank.

Called from heartbeat after T3 normalization. Design:
- T3 MD is the source of truth; Hindsight is a derived index.
- Incremental: only re-sync files whose mtime advanced past the cursor.
- Idempotent: each bullet gets a deterministic document_id so retries
  don't produce duplicates in Hindsight.
- Non-blocking: all errors are caught and logged; heartbeat never fails
  because of a sync hiccup.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.memory.backends.hindsight import HindsightBackend
from app.memory.md_store import extract_entry_lines, parse_entry_line
from app.memory.metrics import record_sync


# Sentinel distinguishing "DB lookup failed" from "tenant explicitly null".
# Failures MUST fail-closed to MD rather than silently inheriting env value,
# otherwise a lookup error would override an operator's explicit opt-out.
class _LookupFailed:
    __slots__ = ()
    def __repr__(self) -> str:
        return "LOOKUP_FAILED"


LOOKUP_FAILED = _LookupFailed()

logger = logging.getLogger(__name__)

_CURSOR_FILENAME = ".hindsight_cursor.json"
_T3_FILES = ("feedback.md", "knowledge.md", "strategies.md", "blocked.md", "user.md")


def _cursor_path(data_root: Path, agent_id: uuid.UUID) -> Path:
    return data_root / str(agent_id) / "memory" / _CURSOR_FILENAME


def _load_cursor(path: Path) -> dict[str, dict[str, float]]:
    """Cursor format: {filename: {"mtime": float, "size": int}}.

    Legacy entries stored as bare floats are auto-migrated on read so upgrades
    don't re-sync every bullet once.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    migrated: dict[str, dict[str, float]] = {}
    for name, val in raw.items():
        if isinstance(val, dict):
            migrated[name] = val
        else:
            migrated[name] = {"mtime": float(val), "size": -1.0}
    return migrated


def _save_cursor(path: Path, cursor: dict[str, dict[str, float]]) -> None:
    """Atomic write to avoid partial cursor on crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".hindsight_cursor.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cursor, f, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError as unlink_exc:
            logger.debug("[HindsightSync] temp cursor cleanup failed: %s", unlink_exc)
        raise


def _document_id(
    agent_id: uuid.UUID,
    category: str,
    content: str,
    timestamp: str,
) -> str:
    """Deterministic content-addressed id for Hindsight dedup.

    Why timestamp is part of the key: two distinct memory events can have
    identical content — e.g. "user prefers terse replies" captured twice at
    different points. Without timestamp in the hash those two entries collide
    on the same document_id and Hindsight silently dedupes one away, losing a
    distinct observation. Timestamp disambiguates.

    Retry safety: the same entry re-read in a subsequent sync produces the
    same (agent, category, timestamp, content) tuple → same ID → safely
    deduped by Hindsight (no duplication on retry).
    """
    raw = f"{agent_id.hex}:{category}:{timestamp}:{content}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


async def _fetch_tenant_backend_pref(
    tenant_id: uuid.UUID,
) -> "str | None | _LookupFailed":
    """Look up Tenant.memory_backend.

    Returns:
    - str ("md" / "hindsight"): explicit per-tenant override
    - None: tenant row exists but column is NULL → env fallback applies
    - LOOKUP_FAILED: DB unavailable → fail-closed, caller MUST coerce to "md"

    Why the sentinel matters: if a tenant has been explicitly opted out to "md",
    a transient DB failure returning None would let env MEMORY_BACKEND=hindsight
    override that choice. That's fail-OPEN and leaks data to an external service.
    """
    try:
        from sqlalchemy import select

        from app.database import async_session
        from app.models.tenant import Tenant

        async with async_session() as db:
            stmt = select(Tenant.memory_backend).where(Tenant.id == tenant_id)
            result = await db.execute(stmt)
            row = result.first()
            if row is None:
                return LOOKUP_FAILED  # tenant not found → safest to stay on MD
            return row[0]
    except Exception as exc:
        logger.warning("[HindsightSync] tenant backend pref lookup failed (fail-closed to MD): %s", exc)
        return LOOKUP_FAILED


def _collect_items(
    agent_id: uuid.UUID,
    memory_dir: Path,
    cursor: dict[str, dict[str, float]],
) -> tuple[list[dict], list[tuple[str, float, int]]]:
    """Return (items_to_sync, files_touched_with_mtime_and_size).

    Skip rule: file is unchanged when BOTH mtime AND size match the cursor.
    Same-mtime appends (coarse filesystems, ext4 without noatime) would
    otherwise silently skip new bullets — size disambiguates.

    files_touched is only used to advance cursor after retain_batch succeeds.
    """
    items: list[dict] = []
    touched: list[tuple[str, float, int]] = []

    for filename in _T3_FILES:
        path = memory_dir / filename
        if not path.exists():
            continue

        try:
            st = path.stat()
            mtime = st.st_mtime
            size = st.st_size
        except OSError:
            continue

        prev = cursor.get(filename)
        if prev is not None:
            prev_mtime = prev.get("mtime", 0.0)
            prev_size = int(prev.get("size", -1))
            if mtime <= prev_mtime and size == prev_size:
                continue  # unchanged by both signals → skip

        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue

        category = path.stem  # feedback.md → "feedback"
        for entry_line in extract_entry_lines(content):
            text, ts = parse_entry_line(entry_line)
            if not text:
                continue
            timestamp = ts or datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            items.append({
                "content": text,
                "category": category,
                "timestamp": timestamp,
                "document_id": _document_id(agent_id, category, text, timestamp),
            })
        touched.append((filename, mtime, size))

    return items, touched


async def sync_t3_to_hindsight(
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    *,
    data_root: Path | None = None,
) -> int:
    """Sync this agent's T3 MD bullets to its Hindsight bank.

    Returns the number of items successfully batched (0 on any failure or
    when Hindsight is not active). Never raises.
    """
    if tenant_id is None:
        return 0  # legacy agent without tenant — skip

    try:
        from app.config import get_settings
        from app.memory.backend import get_memory_backend

        tenant_pref = await _fetch_tenant_backend_pref(tenant_id)
        # isinstance (not `is`) so Pyright narrows the type for the call below.
        if isinstance(tenant_pref, _LookupFailed):
            return 0  # fail-closed: don't sync to Hindsight when tenant state is unknown
        backend = get_memory_backend(
            tenant_id=tenant_id, tenant_backend_pref=tenant_pref,
        )
        if not isinstance(backend, HindsightBackend):
            return 0  # MD backend or misconfigured — nothing to do

        root = data_root or Path(get_settings().AGENT_DATA_DIR)
        memory_dir = root / str(agent_id) / "memory"
        if not memory_dir.exists():
            return 0

        cursor_path = _cursor_path(root, agent_id)
        cursor = _load_cursor(cursor_path)
        items, touched = _collect_items(agent_id, memory_dir, cursor)

        if not items:
            return 0

        ok = await backend.retain_batch(agent_id, items)
        if not ok:
            logger.warning(
                "[HindsightSync] retain_batch returned False for agent=%s tenant=%s — cursor NOT advanced",
                agent_id, tenant_id,
            )
            return 0

        for filename, mtime, size in touched:
            cursor[filename] = {"mtime": mtime, "size": float(size)}
        try:
            _save_cursor(cursor_path, cursor)
        except OSError as exc:
            logger.warning(
                "[HindsightSync] cursor write failed for agent=%s: %s "
                "(items synced but next tick may re-send)",
                agent_id, exc,
            )

        record_sync(tenant_id.hex, len(items))
        logger.info(
            "[HindsightSync] synced %d items across %d files (agent=%s tenant=%s)",
            len(items), len(touched), agent_id, tenant_id,
        )
        return len(items)

    except Exception as exc:
        logger.warning(
            "[HindsightSync] unexpected failure (agent=%s tenant=%s): %s",
            agent_id, tenant_id, exc,
        )
        return 0
