"""Startup replay for the durable extraction queue (P0-2b).

When a process dies between `extract_queue.enqueue` and the corresponding
`mark_done` (deploy restart, OOM, crash mid-LLM call, drain timeout) the
payload remains on disk. On the next startup `replay_pending_extractions`
scans the queue and re-schedules each surviving entry through the normal
`extract_agent.schedule_extract` path.

The old entry is removed only after `schedule_extract(require_durable_enqueue=True)`
confirms it wrote a fresh queue entry for the same payload. If that fresh
enqueue fails, the original payload remains on disk for the next startup.

Stale entries (older than ``max_age_seconds``) are skipped — operators
can inspect or purge them via ``extract_queue.purge_older_than``.
"""
from __future__ import annotations

import logging
import uuid

from app.services import extract_queue

logger = logging.getLogger(__name__)


# Default: re-attempt anything queued in the last week.
_DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600


async def replay_pending_extractions(
    *,
    max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, int]:
    """Re-schedule extractions that didn't reach mark_done last run.

    Returns a counter dict: ``{"scheduled": N, "skipped_stale": N, "failed": N}``.

    Caller (main.py lifespan) treats failures as non-fatal — entries that
    couldn't be re-scheduled stay on disk for the next attempt.
    """
    # Imported lazily so unit tests for this module can monkeypatch the
    # extract_agent singleton without pulling in its full DB-touching graph.
    from app.services.extract_agent import extract_agent

    scheduled = 0
    failed = 0
    seen_in_window: set[str] = set()

    for entry in extract_queue.list_pending(max_age_seconds=max_age_seconds):
        seen_in_window.add(entry.entry_id)
        try:
            tenant_uuid = uuid.UUID(entry.tenant_id) if entry.tenant_id else None
            agent_uuid = uuid.UUID(entry.agent_id)
        except ValueError as exc:
            logger.warning(
                "[QueueReplay] Skipping entry %s with malformed UUID: %s",
                entry.entry_id,
                exc,
            )
            failed += 1
            continue

        try:
            durable_scheduled = extract_agent.schedule_extract(
                agent_id=agent_uuid,
                messages=entry.messages,
                # Tag replay source so downstream metrics can distinguish
                # restart-driven retries from fresh hot-path extractions.
                source=f"replay:{entry.source}",
                tenant_id=tenant_uuid,
                agent_name=entry.agent_name,
                require_durable_enqueue=True,
            )
        except Exception as exc:
            # schedule_extract itself failed (extremely rare — usually only
            # asyncio loop misuse). Leave the original entry in place.
            logger.exception(
                "[QueueReplay] schedule_extract raised for entry %s: %s — leaving entry for next attempt",
                entry.entry_id,
                exc,
            )
            failed += 1
            continue
        if durable_scheduled is not True:
            logger.error(
                "[QueueReplay] schedule_extract returned without a fresh durable queue entry for %s — leaving "
                "original entry for next attempt",
                entry.entry_id,
            )
            failed += 1
            continue

        # New schedule has its own queue entry (P0-2a contract); drop the
        # old one so we don't re-replay it next startup.
        extract_queue.mark_done(entry.entry_id)
        scheduled += 1

    # Anything left on disk older than the window is stale — count without
    # touching it. Operators can purge via extract_queue.purge_older_than.
    all_pending = list(extract_queue.list_pending())
    skipped_stale = sum(1 for e in all_pending if e.entry_id not in seen_in_window)

    return {"scheduled": scheduled, "skipped_stale": skipped_stale, "failed": failed}
