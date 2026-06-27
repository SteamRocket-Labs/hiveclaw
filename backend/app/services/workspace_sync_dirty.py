"""Dirty-flag tracking for workspace sync.

Replaces blind periodic polling: producers (API endpoints that mutate
org/A2A data) call mark_tenant_dirty() / mark_agent_dirty().
The consumer (workspace sync loop) only re-syncs what changed.

Multi-instance: marks are broadcast over Redis pub/sub so every backend
replica's local set converges. If Redis is unavailable, falls back to
in-process tracking — the periodic full-sweep is the safety net.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_DIRTY_CHANNEL = "workspace_sync:dirty"

_dirty_tenants: set[uuid.UUID] = set()
_dirty_agents: set[uuid.UUID] = set()
_lock = asyncio.Lock()
_listener_started = False


def _publish_async(payload: dict[str, Any]) -> None:
    """Schedule a Redis publish without blocking the caller. No-op outside an event loop (e.g. sync tests)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        logger.debug("[dirty] no running loop, skipping broadcast %s: %s", payload, exc)
        return
    loop.create_task(_publish(payload))


async def _publish(payload: dict[str, Any]) -> None:
    try:
        from app.core.events import publish_event

        await publish_event(_DIRTY_CHANNEL, payload)
    except Exception as exc:
        logger.debug("[dirty] Redis publish failed (in-process only): %s", exc)


def mark_tenant_dirty(tenant_id: uuid.UUID | None, *, broadcast: bool = True) -> None:
    """Mark a tenant's workspace files (company_profile, org_structure, all agents) as needing re-sync."""
    if tenant_id is None:
        return
    _dirty_tenants.add(tenant_id)
    if broadcast:
        _publish_async({"type": "tenant", "id": str(tenant_id)})


def mark_agent_dirty(agent_id: uuid.UUID | None, *, broadcast: bool = True) -> None:
    """Mark a single agent's workspace projection/cache as needing refresh."""
    if agent_id is None:
        return
    _dirty_agents.add(agent_id)
    if broadcast:
        _publish_async({"type": "agent", "id": str(agent_id)})


async def consume_dirty() -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Atomically swap out and return the dirty sets for processing."""
    async with _lock:
        tenants = _dirty_tenants.copy()
        agents = _dirty_agents.copy()
        _dirty_tenants.clear()
        _dirty_agents.clear()
    return tenants, agents


def snapshot_sizes() -> tuple[int, int]:
    """For tests/diagnostics — current dirty counts without consuming."""
    return len(_dirty_tenants), len(_dirty_agents)


async def start_redis_listener() -> None:
    """Subscribe to dirty broadcasts from peer backend instances. Safe to call repeatedly."""
    global _listener_started
    if _listener_started:
        return
    _listener_started = True

    try:
        from app.core.events import get_redis

        client = await get_redis()
        pubsub = client.pubsub()
        await pubsub.subscribe(_DIRTY_CHANNEL)
    except Exception as exc:
        _listener_started = False
        logger.warning("[dirty] Redis listener unavailable, in-process only: %s", exc)
        return

    async def _listen() -> None:
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                raw = msg.get("data")
                try:
                    payload = json.loads(raw) if isinstance(raw, (str, bytes)) else None
                except (TypeError, ValueError) as parse_err:
                    logger.debug("[dirty] bad payload: %s", parse_err)
                    continue
                if not isinstance(payload, dict):
                    continue
                kind = payload.get("type")
                raw_id = payload.get("id")
                if not isinstance(raw_id, str):
                    continue
                try:
                    ident = uuid.UUID(raw_id)
                except ValueError:
                    continue
                if kind == "tenant":
                    mark_tenant_dirty(ident, broadcast=False)
                elif kind == "agent":
                    mark_agent_dirty(ident, broadcast=False)
        except Exception as exc:
            logger.warning("[dirty] Redis listener exited: %s", exc)

    asyncio.create_task(_listen())
    logger.info("📡 Workspace dirty-flag Redis listener started on channel %s", _DIRTY_CHANNEL)
