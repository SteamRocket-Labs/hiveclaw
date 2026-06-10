"""Self-evolution background daemon — heartbeat + workspace sync.

Decoupled from `trigger_daemon` so a slow heartbeat tick or workspace
volume I/O can't introduce schedule jitter into the trigger tick. Each
loop runs as an independent asyncio task; failures in one do not block
the others. Dream is fired opportunistically by the surfaces that own a
session boundary (trigger end, response complete) rather than being
polled here.

Lifespan wiring lives in `app/main.py` — the daemon is spawned alongside
`start_trigger_daemon` so both come up at server boot.
"""

from __future__ import annotations

import asyncio

from loguru import logger

# Heartbeat tick cadence — read from typed settings so production (60s)
# and dev (lower) configs are explicit. P1-W2-5: configurable via the
# HEARTBEAT_TICK_SECONDS env var bound to `Settings`.
from app.config import get_settings

_HEARTBEAT_INTERVAL_SECONDS = get_settings().HEARTBEAT_TICK_SECONDS


async def _heartbeat_loop() -> None:
    """Run heartbeat ticks at the configured cadence.

    Each tick is wrapped in its own try/except so transient errors never
    take the loop down. Pending-reply cleanup is piggybacked on the same
    cadence (it has no independent timer).
    """
    from app.database import async_session, enter_rls_bypass
    from app.services.heartbeat import _heartbeat_tick
    from app.services.pending_reply_service import cleanup_expired_replies

    while True:
        try:
            await _heartbeat_tick()
        except Exception as e:
            logger.error(f"[EvolutionDaemon] heartbeat tick error: {e}")

        try:
            async with (
                async_session() as db,
                enter_rls_bypass(db, reason="pending-reply expiry sweep — expire stale contexts across all tenants"),
            ):
                await cleanup_expired_replies(db)
                await db.commit()
        except Exception as e:
            logger.debug(f"[EvolutionDaemon] PendingReply cleanup error (non-fatal): {e}")

        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)


async def start_evolution_daemon() -> None:
    """Boot heartbeat + workspace sync loops as independent background tasks.

    Returns once the heartbeat loop is running — the workspace-sync loops
    are detached and run for the lifetime of the process. Errors during
    workspace-sync setup are logged but do not stop heartbeat from
    starting (workspace sync is best-effort persistence; heartbeat is the
    primary self-evolution path).
    """
    logger.info(
        "🌱 Evolution Daemon started (heartbeat every {}s, workspace sync continuous)",
        _HEARTBEAT_INTERVAL_SECONDS,
    )

    try:
        from app.services.heartbeat import _workspace_full_sweep_loop, _workspace_sync_loop
        from app.services.workspace_sync_dirty import start_redis_listener

        await start_redis_listener()
        asyncio.create_task(_workspace_sync_loop(), name="workspace_sync_loop")
        asyncio.create_task(_workspace_full_sweep_loop(), name="workspace_full_sweep_loop")
    except Exception as e:
        logger.error(f"[EvolutionDaemon] Failed to spawn workspace sync loops: {e}")

    await _heartbeat_loop()
