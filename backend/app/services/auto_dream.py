"""Auto-Dream — background MD-first memory consolidation service.

Dream works on canonical markdown layers:
  - T2 learnings (`memory/learnings/*.md`)
  - T3 durable memory (`memory/*.md`)
  - soul.md for high-signal identity promotion

The runtime now uses a programmatic md-only consolidation path. Legacy semantic
memory code has been removed from the primary service so dream stays aligned
with the md-first architecture.
"""

from __future__ import annotations

import logging
import json
import re as _re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

# Consolidation gates — tuned for active agents that run heartbeats/triggers.
# Both conditions must be met: enough time elapsed AND enough new sessions.
MIN_HOURS_BETWEEN_DREAMS = 4  # B4 fix: lowered from 6 for better coverage
MIN_SESSIONS_SINCE_DREAM = 3

# Soft dream: lightweight maintenance (dedup + index/shadow refresh, no LLM)
# Triggers when facts approach the 150 cap but full dream gate isn't met yet.
_SOFT_DREAM_FACT_THRESHOLD = 100
_MIN_HOURS_BETWEEN_SOFT_DREAMS = 2

# Per-agent tracking (in-memory, resets on process restart)
_last_dream_time: dict[str, datetime] = {}
_sessions_since_dream: dict[str, int] = {}

# Prompt contract kept for tests/docs. Runtime dream path is programmatic md-only.
_AUTO_DREAM_SYSTEM_PROMPT = (
    "You consolidate an agent's long-term memory into a clean, deduplicated fact list.\n"
    "Focus on durable reusable facts, strategy lessons, and blocked patterns.\n"
    "Skip transient task state, temporary TODOs, and raw session transcripts.\n"
    "Return only a JSON array — no prose, no explanation."
)

# Dream gate expansion: heartbeat ticks also count toward triggering dreams
MIN_HEARTBEAT_TICKS_SINCE_DREAM = 2
_heartbeat_ticks_since_dream: dict[str, int] = {}

# DREAM.md template path
_DREAM_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "DREAM.md"

# ── T3 MD read/write/dedup functions (Phase 6) ──

_T3_FILES = ["feedback.md", "knowledge.md", "strategies.md", "blocked.md", "user.md"]
_T3_MAX_ENTRIES_PER_FILE = 50


def _read_all_t3(agent_id: uuid.UUID) -> dict[str, str]:
    """Read all T3 memory files. Returns {filename: content}."""
    memory_dir = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "memory"
    result: dict[str, str] = {}
    for fname in _T3_FILES:
        fpath = memory_dir / fname
        if fpath.exists():
            try:
                result[fname] = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                logger.warning("[Dream] Failed to read T3 %s: %s", fpath, exc)
    return result


def _write_t3_file(agent_id: uuid.UUID, filename: str, content: str) -> None:
    """Write a T3 memory file."""
    fpath = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "memory" / filename
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(content, encoding="utf-8")


def _programmatic_dedup(lines: list[str], similarity_threshold: float = 0.7) -> list[str]:
    """Remove near-duplicate lines using SequenceMatcher. Zero LLM dependency."""
    from difflib import SequenceMatcher

    if len(lines) <= 1:
        return lines

    kept: list[str] = []
    for line in lines:
        is_dup = False
        line_lower = line.lower().strip()
        for existing in kept:
            ratio = SequenceMatcher(None, line_lower, existing.lower().strip()).ratio()
            if ratio >= similarity_threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(line)
    return kept


def _consolidate_t3_files(agent_id: uuid.UUID) -> dict[str, int]:
    """Programmatic T3 consolidation: dedup + cap per file. Returns {filename: entries_removed}."""
    stats: dict[str, int] = {}
    t3_files = _read_all_t3(agent_id)

    for fname, content in t3_files.items():
        lines = content.strip().splitlines()
        # Separate header from entry lines
        header_lines: list[str] = []
        entry_lines: list[str] = []
        for line in lines:
            if line.startswith("- [") or line.startswith("- "):
                entry_lines.append(line)
            else:
                if not entry_lines:
                    header_lines.append(line)
                else:
                    entry_lines.append(line)

        before = len(entry_lines)
        # Dedup
        deduped = _programmatic_dedup(entry_lines)
        # Cap: keep most recent (last N entries)
        if len(deduped) > _T3_MAX_ENTRIES_PER_FILE:
            deduped = deduped[-_T3_MAX_ENTRIES_PER_FILE:]

        after = len(deduped)
        removed = before - after

        if removed > 0:
            new_content = "\n".join(header_lines + deduped) + "\n"
            _write_t3_file(agent_id, fname, new_content)
            logger.info("[Dream] T3 %s: %d → %d entries (%d removed)", fname, before, after, removed)
        stats[fname] = removed

    return stats


def _truncate_t2(agent_id: uuid.UUID, keep: int = 10) -> int:
    """Truncate T2 learnings files to keep only the most recent N entries each."""
    learnings_dir = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "memory" / "learnings"
    if not learnings_dir.exists():
        return 0

    total_removed = 0
    for fname in ["insights.md", "errors.md", "requests.md"]:
        fpath = learnings_dir / fname
        if not fpath.exists():
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            lines = content.strip().splitlines()
            header: list[str] = []
            entries: list[str] = []
            for line in lines:
                if line.startswith("- ["):
                    entries.append(line)
                elif not entries:
                    header.append(line)

            if len(entries) > keep:
                removed = len(entries) - keep
                total_removed += removed
                entries = entries[-keep:]
                fpath.write_text("\n".join(header + entries) + "\n", encoding="utf-8")
                logger.info("[Dream] T2 %s truncated: kept %d, removed %d", fname, keep, removed)
        except Exception as exc:
            logger.warning("[Dream] Failed to truncate T2 %s: %s", fname, exc)

    return total_removed


def _update_index_md(agent_id: uuid.UUID) -> None:
    """Regenerate memory/INDEX.md from current T3 file contents."""
    from app.memory.md_store import rebuild_index

    rebuild_index(Path(get_settings().AGENT_DATA_DIR), agent_id)


def _count_t3_entries(agent_id: uuid.UUID) -> int:
    from app.memory.md_store import extract_entry_lines

    total = 0
    for content in _read_all_t3(agent_id).values():
        total += len(extract_entry_lines(content))
    return total


def _promote_repeated_feedback_to_soul(agent_id: uuid.UUID, feedback_content: str) -> dict:
    """Promote repeated feedback patterns to soul.md without LLM.

    Returns:
        {"count": int, "decisions": list[dict]}
        decisions[i] = {soul_excerpt, source_t3_file, repetition_count, reason}

    Callers may treat the return as int via dict["count"] or via the
    isinstance() guard in run_dream() for backwards-compat.
    """
    from difflib import SequenceMatcher

    from app.memory.md_store import extract_entry_lines, parse_entry_line

    raw_entries = [parse_entry_line(line)[0] for line in extract_entry_lines(feedback_content)]
    if len(raw_entries) < 3:
        return {"count": 0, "decisions": []}

    clusters: list[dict[str, object]] = []
    for entry in raw_entries:
        normalized = entry.strip().lower()
        if not normalized:
            continue
        matched = False
        for cluster in clusters:
            representative = str(cluster["representative"])
            similarity = SequenceMatcher(None, representative, normalized).ratio()
            if similarity >= 0.78:
                cluster["count"] = int(cluster["count"]) + 1
                longest = str(cluster["content"])
                if len(entry) > len(longest):
                    cluster["content"] = entry
                matched = True
                break
        if not matched:
            clusters.append(
                {
                    "representative": normalized,
                    "content": entry,
                    "count": 1,
                }
            )

    promotable_clusters = [c for c in clusters if int(c["count"]) >= 3]
    if not promotable_clusters:
        return {"count": 0, "decisions": []}

    soul_path = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "soul.md"
    existing = soul_path.read_text(encoding="utf-8", errors="replace") if soul_path.exists() else "# Soul\n\n"
    existing_lower = existing.lower()
    new_clusters = [
        c for c in promotable_clusters if str(c["content"]).lower() not in existing_lower
    ]
    if not new_clusters:
        return {"count": 0, "decisions": []}

    new_behaviors = [str(c["content"]) for c in new_clusters]
    header = "## Learned Behaviors"
    behavior_block = "\n".join(f"- {content}" for content in new_behaviors) + "\n"
    if header in existing:
        insert_at = existing.index(header) + len(header)
        updated = existing[:insert_at] + "\n" + behavior_block + existing[insert_at:]
    else:
        updated = existing.rstrip() + f"\n\n{header}\n" + behavior_block

    soul_path.write_text(updated.strip() + "\n", encoding="utf-8")

    decisions = [
        {
            "soul_excerpt": str(c["content"]),
            "source_t3_file": "feedback.md",
            "repetition_count": int(c["count"]),
            "reason": "feedback repeated 3+ times → promoted to soul",
        }
        for c in new_clusters
    ]
    return {"count": len(new_behaviors), "decisions": decisions}


def record_heartbeat_tick(agent_id: uuid.UUID) -> None:
    """Increment heartbeat tick counter for dream gate evaluation."""
    key = agent_id.hex
    _heartbeat_ticks_since_dream[key] = _heartbeat_ticks_since_dream.get(key, 0) + 1


def _build_dream_consolidation_prompt(*, facts: list[dict], summaries: list[str]) -> str:
    """Legacy prompt contract kept for validation tests and human inspection."""
    facts_text = "\n".join(
        str(i) + ". [" + f.get("category", "general") + "] " + f.get("content", "")[:200] for i, f in enumerate(facts)
    )
    summaries_text = "\n---\n".join(s[:500] for s in summaries[:5])
    return (
        "You are consolidating an agent's long-term memory.\n\n"
        "## Current Facts\n" + facts_text + "\n\n"
        "## Recent Session Summaries\n" + summaries_text + "\n\n"
        "## Instructions\n"
        "1. Remove duplicate or contradictory facts (keep the newer/more specific one)\n"
        "2. Merge related facts into single comprehensive statements\n"
        "3. Add new facts from sessions that aren't already captured\n"
        "4. Assign each fact a category: user, feedback, project, reference, constraint, strategy, blocked_pattern, or general\n"
        "5. When facts contradict each other, keep the one from a more recent session summary\n"
        "6. Each fact should be concise (under 200 characters) — merge verbose entries into crisp statements\n"
        "7. Promote durable successful approaches to strategy\n"
        "8. Promote repeated failed approaches to blocked_pattern\n"
        "9. evolution files remain the home for active policy iteration; keep only the durable outcome here\n\n"
        "## What NOT to consolidate\n"
        "- Ephemeral task details (in-progress work, temporary state) — these belong in focus.md, not memory\n"
        "- Code patterns or file paths that can be derived by reading the workspace\n"
        "- Debugging solutions — the fix should be in the code, not in memory\n"
        "- Exact tool call sequences — only outcomes and learnings matter\n\n"
        "Return ONLY the JSON array, no other text."
    )


def _simple_dedup(facts: list[dict]) -> list[dict]:
    """Deterministic content dedup helper kept for tests and maintenance tasks."""
    seen: set[str] = set()
    unique: list[dict] = []
    for fact in facts:
        content = str(fact.get("content", "")).strip().lower()
        if not content or content in seen:
            continue
        seen.add(content)
        unique.append(fact)
    return unique


def _dream_state_path(agent_id: uuid.UUID) -> Path:
    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "memory" / "auto_dream_state.json"


_dream_version: dict[str, int] = {}
_dream_history: dict[str, list[dict]] = {}
_DREAM_HISTORY_MAX = 10


def _load_dream_state(agent_id: uuid.UUID) -> tuple[datetime | None, int]:
    key = agent_id.hex
    if key in _sessions_since_dream or key in _last_dream_time:
        return _last_dream_time.get(key), _sessions_since_dream.get(key, 0)

    path = _dream_state_path(agent_id)
    if not path.exists():
        return None, 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[AutoDream] Failed to load dream state: %s", exc)
        return None, 0

    last_raw = payload.get("last_dream_time")
    sessions = payload.get("sessions_since_dream", 0)
    last = None
    if isinstance(last_raw, str):
        try:
            parsed = datetime.fromisoformat(last_raw)
            # Ensure timezone-aware — naive datetimes cause TypeError in should_dream()
            last = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            last = None
    if last is not None:
        _last_dream_time[key] = last
    _sessions_since_dream[key] = sessions if isinstance(sessions, int) else 0
    _dream_version[key] = payload.get("version", 0)
    _dream_history[key] = payload.get("history", [])
    return _last_dream_time.get(key), _sessions_since_dream.get(key, 0)


def _persist_dream_state(agent_id: uuid.UUID) -> None:
    key = agent_id.hex
    path = _dream_state_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    last_time = _last_dream_time.get(key)
    payload = {
        "last_dream_time": last_time.isoformat() if last_time else None,
        "sessions_since_dream": _sessions_since_dream.get(key, 0),
        "version": _dream_version.get(key, 0),
        "history": _dream_history.get(key, [])[-_DREAM_HISTORY_MAX:],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def record_session_end(agent_id: uuid.UUID) -> None:
    """Increment session counter for dream gate evaluation."""
    key = agent_id.hex
    _, sessions = _load_dream_state(agent_id)
    _sessions_since_dream[key] = sessions + 1
    _persist_dream_state(agent_id)


def should_dream(agent_id: uuid.UUID) -> bool:
    """Check if time gate + (session OR heartbeat tick) gates are met for consolidation."""
    last, sessions = _load_dream_state(agent_id)
    if last is not None:
        hours_since = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if hours_since < MIN_HOURS_BETWEEN_DREAMS:
            return False

    # Expanded gate: sessions OR heartbeat ticks
    ticks = _heartbeat_ticks_since_dream.get(agent_id.hex, 0)
    return sessions >= MIN_SESSIONS_SINCE_DREAM or ticks >= MIN_HEARTBEAT_TICKS_SINCE_DREAM


def should_soft_dream(agent_id: uuid.UUID) -> bool:
    """Check if a lightweight soft dream should run.

    Triggers when T3 memory is approaching the 150-entry cap but the full
    dream gate isn't met. Only does programmatic dedup + index/shadow refresh.
    """
    last, sessions = _load_dream_state(agent_id)
    if sessions < 1:
        return False
    # Don't soft-dream if full dream is about to trigger
    if sessions >= MIN_SESSIONS_SINCE_DREAM:
        return False
    # Time gate for soft dream
    if last is not None:
        hours_since = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if hours_since < _MIN_HOURS_BETWEEN_SOFT_DREAMS:
            return False
    return _count_t3_entries(agent_id) >= _SOFT_DREAM_FACT_THRESHOLD


async def run_soft_dream(agent_id: uuid.UUID) -> dict:
    """Lightweight maintenance: dedup + index/shadow refresh without LLM calls.

    Runs between full dreams to prevent fact accumulation and keep T3 fresh.
    """
    before_count = _count_t3_entries(agent_id)
    if before_count == 0:
        return {"soft_dream": True, "consolidated": 0, "removed": 0}

    t3_stats = _consolidate_t3_files(agent_id)
    removed = sum(t3_stats.values())
    _update_index_md(agent_id)
    after_count = _count_t3_entries(agent_id)

    logger.info(
        "[AutoDream] Soft dream for %s: %d → %d T3 entries (%d deduped)",
        agent_id,
        before_count,
        after_count,
        removed,
    )
    return {"soft_dream": True, "consolidated": after_count, "removed": removed}


async def run_dream(agent_id: uuid.UUID, tenant_id: uuid.UUID) -> dict:
    """Execute memory consolidation for an agent.

    Returns a summary dict with keys: consolidated, removed, added.
    """
    key = agent_id.hex
    del tenant_id
    t3_files = _read_all_t3(agent_id)
    if not t3_files:
        _mark_dreamed(key)
        return {"consolidated": 0, "removed": 0, "added": 0}

    before_count = _count_t3_entries(agent_id)
    promotion_result = _promote_repeated_feedback_to_soul(agent_id, t3_files.get("feedback.md", ""))
    if isinstance(promotion_result, dict):
        promoted_to_soul = int(promotion_result.get("count", 0))
        promotion_decisions = promotion_result.get("decisions") or []
    else:
        # Backwards-compat for the legacy int return type.
        promoted_to_soul = int(promotion_result)
        promotion_decisions = []
    t3_stats = _consolidate_t3_files(agent_id)
    t3_removed = sum(t3_stats.values())
    dedup_decisions = [
        {
            "file": fname,
            "kept": "(consolidated)",
            "dropped_count": removed,
            "reason": f"dedup+cap={removed}",
        }
        for fname, removed in t3_stats.items()
        if removed
    ]
    _update_index_md(agent_id)
    after_count = _count_t3_entries(agent_id)
    t2_removed = _truncate_t2(agent_id, keep=10)
    if t3_removed or t2_removed:
        logger.info(
            "[AutoDream] MD consolidation for %s: T3 deduped %d, T2 truncated %d",
            agent_id,
            t3_removed,
            t2_removed,
        )

    _cleanup_focus(agent_id)
    _review_blocklist(agent_id)

    _mark_dreamed(
        key,
        consolidation_result={
            "facts_before": before_count,
            "facts_after": after_count,
            "strategy": "md_only",
            "clusters": 0,
        },
    )

    # T0 cleanup/audit: preserve the raw transcript substrate and backfill when older sessions exist without T0 files.
    from app.services.t0_logger import audit_t0_logs, backfill_recent_chat_logs, cleanup_old_logs

    cleanup_old_logs(agent_id, retention_days=30)
    t0_audit = audit_t0_logs(agent_id, recent_days=30)
    t0_backfill = {"sessions_scanned": 0, "written": 0, "skipped_existing": 0, "skipped_empty": 0}
    if t0_audit["recent_files"] == 0:
        t0_backfill = await backfill_recent_chat_logs(agent_id, recent_days=30, limit_sessions=20)
        t0_audit = audit_t0_logs(agent_id, recent_days=30)

    # Reset heartbeat tick counter (dream completes the cycle)
    _heartbeat_ticks_since_dream.pop(key, None)

    result = {
        "consolidated": after_count,
        "removed": max(0, before_count - after_count),
        "added": promoted_to_soul,
        "t3_deduped": t3_removed,
        "t2_truncated": t2_removed,
        "t0_recent_files": t0_audit["recent_files"],
        "t0_backfilled": t0_backfill["written"],
    }

    # Emit DREAM_END hook → T0 log + heartbeat session reset
    try:
        from app.runtime.hooks import HookEvent, emit_hook

        await emit_hook(
            HookEvent.DREAM_END,
            agent_id=agent_id,
            source="dream",
            metadata={
                "t3_processed": after_count,
                "deduped": t3_removed,
                "promoted_to_soul": promoted_to_soul,
                "strategy": "md_only",
                "t2_truncated": t2_removed,
                "dedup_decisions": dedup_decisions,
                "promotion_decisions": promotion_decisions,
                "cleanup_summary": (
                    f"focus cleaned + blocklist reviewed; T2 truncated {t2_removed}"
                ),
            },
        )
    except Exception as _hook_err:
        logger.debug("[AutoDream] DREAM_END hook failed (non-fatal): %s", _hook_err)

    logger.info(
        "[AutoDream] Consolidated memory for %s: %d → %d facts (%d removed, %d added, strategy=%s, clusters=%d, t3_dedup=%d, t2_trunc=%d)",
        agent_id,
        before_count,
        after_count,
        result["removed"],
        result["added"],
        "md_only",
        0,
        t3_removed,
        t2_removed,
    )
    return result


_DREAM_BACKUP_MAX = 3


def _backup_facts(agent_id: uuid.UUID, facts: list[dict]) -> None:
    """Write a timestamped backup of facts before consolidation. Keep last 3."""
    backup_dir = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "memory" / "dream_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"dream_backup_{stamp}.json"
    try:
        backup_path.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("[AutoDream] Failed to write backup: %s", exc)
        return

    # Rotate: keep only the most recent backups
    backups = sorted(backup_dir.glob("dream_backup_*.json"), key=lambda p: p.name)
    for old in backups[:-_DREAM_BACKUP_MAX]:
        try:
            old.unlink()
        except OSError as rm_err:
            logger.debug("[AutoDream] Failed to remove old backup %s: %s", old.name, rm_err)


def _mark_dreamed(
    key: str,
    *,
    consolidation_result: dict | None = None,
) -> None:
    _last_dream_time[key] = datetime.now(timezone.utc)
    sessions_processed = _sessions_since_dream.get(key, 0)
    _sessions_since_dream[key] = 0

    # Increment version and record history entry
    prev_version = _dream_version.get(key, 0)
    _dream_version[key] = prev_version + 1

    if consolidation_result:
        history_entry = {
            "version": _dream_version[key],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "facts_before": consolidation_result.get("facts_before", 0),
            "facts_after": consolidation_result.get("facts_after", 0),
            "sessions_processed": sessions_processed,
            "strategy": consolidation_result.get("strategy", "unknown"),
            "clusters": consolidation_result.get("clusters", 0),
        }
        _dream_history.setdefault(key, []).append(history_entry)
        # Trim to keep only recent history
        _dream_history[key] = _dream_history[key][-_DREAM_HISTORY_MAX:]

    try:
        _persist_dream_state(uuid.UUID(hex=key))
    except Exception:
        logger.debug("[AutoDream] Failed to persist dream state for %s", key)

# ── Focus cleanup: remove stale items from focus.md (断点 B8 fix) ──

_FOCUS_MAX_AGE_DAYS = 7
_FOCUS_MAX_CHARS = 3000

_DATE_PATTERN = _re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")


def _cleanup_focus(agent_id: uuid.UUID) -> None:
    """Remove stale items from focus.md to prevent Working Memory bloat.

    Removes:
    - Items with dates older than _FOCUS_MAX_AGE_DAYS
    - Completed checkbox items (- [x])
    - Truncates to _FOCUS_MAX_CHARS if still too large
    """
    from app.services.heartbeat import _get_canonical_workspace

    ws_root = _get_canonical_workspace(agent_id)
    if not ws_root:
        ws_root = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)

    focus_path = ws_root / "focus.md"
    if not focus_path.exists():
        return

    try:
        content = focus_path.read_text(encoding="utf-8")
    except Exception as read_err:
        logger.debug("[AutoDream] Failed to read focus.md for cleanup: %s", read_err)
        return

    from app.services.focus_state import parse_focus_tasks

    completed_task_lines = {task.raw_line for task in parse_focus_tasks(content) if task.completed}

    lines = content.splitlines()
    if len(lines) < 5:
        return  # Too small to need cleanup

    now = datetime.now(timezone.utc).date()
    kept: list[str] = []
    removed_count = 0

    for line in lines:
        stripped = line.strip()

        # Remove completed checkboxes
        if stripped in completed_task_lines:
            removed_count += 1
            continue

        # Remove items with expired dates
        date_match = _DATE_PATTERN.search(stripped)
        if date_match:
            try:
                item_date = datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
                age_days = (now - item_date).days
                if age_days > _FOCUS_MAX_AGE_DAYS:
                    removed_count += 1
                    continue
            except ValueError as date_err:
                logger.debug("[AutoDream] Malformed date in focus.md, keeping line: %s", date_err)

        kept.append(line)

    if removed_count == 0:
        # No stale items found; check size only
        if len(content) <= _FOCUS_MAX_CHARS:
            return
        # Truncate from the middle, keep header + tail
        kept = kept[:3] + ["", "(older items removed by auto-dream)", ""] + kept[-10:]

    cleaned = "\n".join(kept)
    if len(cleaned) > _FOCUS_MAX_CHARS:
        cleaned = cleaned[:_FOCUS_MAX_CHARS] + "\n...(truncated by auto-dream)\n"

    try:
        focus_path.write_text(cleaned, encoding="utf-8")
        logger.info("[AutoDream] Cleaned focus.md for %s: removed %d stale items", agent_id, removed_count)
    except Exception as exc:
        logger.debug("[AutoDream] Failed to clean focus.md: %s", exc)


# ── Blocklist review: expire old entries (断点 B6 fix) ──

_BLOCKLIST_EXPIRY_DAYS = 60
_BLOCKLIST_DATE_RE = _re.compile(r"^\s*-\s*\[(\d{4}-\d{2}-\d{2})\]")


def _review_blocklist(agent_id: uuid.UUID) -> None:
    """Remove expired blocklist entries (older than _BLOCKLIST_EXPIRY_DAYS).

    Conservative approach: no LLM needed, just date-based expiry.
    Old blocked patterns may no longer be relevant after environment changes.
    """
    from app.services.heartbeat import _get_canonical_workspace

    ws_root = _get_canonical_workspace(agent_id)
    if not ws_root:
        ws_root = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)

    blocklist_path = ws_root / "evolution" / "blocklist.md"
    if not blocklist_path.exists():
        return

    try:
        content = blocklist_path.read_text(encoding="utf-8", errors="replace")
    except Exception as read_err:
        logger.debug("[AutoDream] Failed to read blocklist.md: %s", read_err)
        return

    lines = content.splitlines()
    now = datetime.now(timezone.utc).date()
    kept: list[str] = []
    expired_count = 0

    for line in lines:
        date_match = _BLOCKLIST_DATE_RE.match(line)
        if date_match:
            try:
                entry_date = datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
                age_days = (now - entry_date).days
                if age_days > _BLOCKLIST_EXPIRY_DAYS:
                    expired_count += 1
                    continue
            except ValueError as date_err:
                logger.debug("[AutoDream] Malformed blocklist date: %s", date_err)
        kept.append(line)

    if expired_count == 0:
        return

    try:
        blocklist_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        logger.info(
            "[AutoDream] Expired %d blocklist entries for %s (>%d days)",
            expired_count,
            agent_id,
            _BLOCKLIST_EXPIRY_DAYS,
        )
    except Exception as write_err:
        logger.debug("[AutoDream] Failed to write blocklist.md: %s", write_err)
