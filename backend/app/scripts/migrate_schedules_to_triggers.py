"""Migrate existing AgentSchedule records to AgentTrigger (cron type).

Run this script once after deploying Phase 2 of the Aware engine.
It converts all existing agent_schedules into agent_triggers with type='cron'.

Usage:
    python -m app.scripts.migrate_schedules_to_triggers
"""

import asyncio

from loguru import logger
from sqlalchemy import select

from app.database import async_session, enter_rls_bypass
from app.models.agent import Agent  # noqa: F401 — needed for FK resolution
from app.models.schedule import AgentSchedule
from app.models.trigger import AgentTrigger


async def migrate():
    """Convert all AgentSchedule records to AgentTrigger(type='cron')."""
    # One-time ops migration across every agent's schedules — needs cross-tenant
    # visibility, so run under an explicit audited bypass.
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="one-time schedule→trigger migration across all agents") as bdb,
    ):
        db = bdb
        result = await db.execute(
            select(
                AgentSchedule.id,
                AgentSchedule.agent_id,
                AgentSchedule.name,
                AgentSchedule.instruction,
                AgentSchedule.cron_expr,
                AgentSchedule.is_enabled,
                AgentSchedule.run_count,
                AgentSchedule.last_run_at,
            )
        )
        schedules = result.all()

        if not schedules:
            logger.info("No schedules found to migrate.")
            return

        migrated = 0
        skipped = 0
        for row in schedules:
            schedule_id, agent_id, name, instruction, cron_expr, is_enabled, run_count, last_run_at = row
            # Check if trigger already exists for this schedule
            existing = await db.execute(
                select(AgentTrigger).where(
                    AgentTrigger.agent_id == agent_id,
                    AgentTrigger.name == f"migrated_{name[:80]}",
                )
            )
            if existing.scalar_one_or_none():
                logger.info(f"  Skip: '{name}' already migrated")
                skipped += 1
                continue

            trigger = AgentTrigger(
                agent_id=agent_id,
                name=f"migrated_{name[:80]}",
                type="cron",
                config={"expr": cron_expr},
                reason=instruction[:500] if instruction else f"Migrated schedule: {name}",
                is_enabled=is_enabled,
                fire_count=run_count or 0,
                last_fired_at=last_run_at,
            )
            db.add(trigger)
            migrated += 1
            logger.info(f"  Migrated: '{name}' → cron({cron_expr})")

        await db.commit()
        logger.info(f"Migration complete: {migrated} migrated, {skipped} skipped")


if __name__ == "__main__":
    asyncio.run(migrate())
