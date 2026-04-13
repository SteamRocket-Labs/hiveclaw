"""Legacy AgentSchedule -> AgentTrigger migration helpers."""

from __future__ import annotations

import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import AgentSchedule
from app.models.trigger import AgentTrigger
from app.services.schedule_surface import build_schedule_trigger_config


async def migrate_legacy_schedules(db: AsyncSession, agent_id: uuid.UUID) -> int:
    """Promote legacy AgentSchedule rows into compat AgentTrigger rows in-place."""
    legacy_result = await db.execute(select(AgentSchedule).where(AgentSchedule.agent_id == agent_id))
    legacy_schedules = list(legacy_result.scalars().all())
    if not legacy_schedules:
        return 0

    trigger_result = await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == agent_id))
    existing_triggers = {trigger.id: trigger for trigger in trigger_result.scalars().all()}

    migrated = 0
    for legacy in legacy_schedules:
        compat_config = build_schedule_trigger_config(
            instruction=legacy.instruction,
            cron_expr=legacy.cron_expr,
            delivery_target_json=legacy.delivery_target_json,
            created_by=legacy.created_by,
        )
        trigger = existing_triggers.get(legacy.id)
        if trigger is None:
            trigger = AgentTrigger(
                id=legacy.id,
                agent_id=legacy.agent_id,
                name=legacy.name,
                type="cron",
                config=compat_config,
                reason=legacy.instruction,
                is_enabled=legacy.is_enabled,
                last_fired_at=legacy.last_run_at,
                fire_count=legacy.run_count or 0,
                created_at=legacy.created_at,
            )
            db.add(trigger)
        else:
            trigger.name = legacy.name
            trigger.type = "cron"
            trigger.config = compat_config
            trigger.reason = legacy.instruction
            trigger.is_enabled = legacy.is_enabled
            trigger.last_fired_at = legacy.last_run_at
            trigger.fire_count = legacy.run_count or 0
            trigger.created_at = legacy.created_at
        await db.delete(legacy)
        migrated += 1
    return migrated
