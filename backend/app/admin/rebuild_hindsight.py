"""Rebuild a tenant's Hindsight bank from canonical T3 markdown.

Usage:
    python -m app.admin.rebuild_hindsight --tenant-id <uuid>
    python -m app.admin.rebuild_hindsight --tenant-id <uuid> --agent-id <uuid>

Walks every agent belonging to the tenant (or just one when --agent-id is
given), force-resets its per-agent sync cursor, and re-uploads every T3 MD
bullet. Idempotent via Hindsight's document_id dedup. Safe to run anytime.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.memory.hindsight_sync import _CURSOR_FILENAME, sync_t3_to_hindsight
from app.models.agent import Agent

logger = logging.getLogger(__name__)


def _reset_cursor(data_root: Path, agent_id: uuid.UUID) -> None:
    cursor = data_root / str(agent_id) / "memory" / _CURSOR_FILENAME
    if cursor.exists():
        cursor.unlink()
        logger.info("Cleared cursor %s", cursor)


async def _agents_in_tenant(
    tenant_id: uuid.UUID, agent_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    async with async_session() as db:
        stmt = select(Agent.id).where(Agent.tenant_id == tenant_id)
        if agent_id is not None:
            stmt = stmt.where(Agent.id == agent_id)
        rows = (await db.execute(stmt)).all()
    return [row[0] for row in rows]


async def rebuild(tenant_id: uuid.UUID, agent_id: uuid.UUID | None) -> int:
    data_root = Path(get_settings().AGENT_DATA_DIR)
    agent_ids = await _agents_in_tenant(tenant_id, agent_id)
    if not agent_ids:
        logger.warning("No agents matched tenant=%s agent=%s", tenant_id, agent_id)
        return 0

    total_items = 0
    for aid in agent_ids:
        _reset_cursor(data_root, aid)
        synced = await sync_t3_to_hindsight(aid, tenant_id, data_root=data_root)
        logger.info("Rebuilt agent=%s → %d items", aid, synced)
        total_items += synced
    logger.info(
        "Rebuild complete: tenant=%s agents=%d items=%d",
        tenant_id, len(agent_ids), total_items,
    )
    return total_items


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Rebuild Hindsight bank from T3 MD.")
    ap.add_argument("--tenant-id", required=True, type=uuid.UUID,
                    help="Tenant UUID whose banks to rebuild.")
    ap.add_argument("--agent-id", type=uuid.UUID, default=None,
                    help="Optional single-agent scope.")
    ap.add_argument("--verbose", "-v", action="store_true")
    return ap.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(rebuild(args.tenant_id, args.agent_id))


if __name__ == "__main__":
    main()
