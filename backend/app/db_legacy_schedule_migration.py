"""Database-level promotion of legacy agent_schedules rows into agent_triggers."""

from __future__ import annotations

from sqlalchemy import MetaData, Table, delete, inspect, select, update
from sqlalchemy.engine import Connection

from app.services.schedule_surface import build_schedule_trigger_config

LEGACY_SCHEDULE_TABLE_NAME = "agent_schedules"
TRIGGER_TABLE_NAME = "agent_triggers"


def promote_legacy_schedules_to_triggers(connection: Connection) -> int:
    """Promote legacy schedule rows into trigger rows, then drop the legacy table."""
    inspector = inspect(connection)
    if not inspector.has_table(LEGACY_SCHEDULE_TABLE_NAME):
        return 0
    if not inspector.has_table(TRIGGER_TABLE_NAME):
        raise RuntimeError("agent_triggers table must exist before migrating legacy schedules")

    metadata = MetaData()
    legacy_table = Table(LEGACY_SCHEDULE_TABLE_NAME, metadata, autoload_with=connection)
    trigger_table = Table(TRIGGER_TABLE_NAME, metadata, autoload_with=connection)

    legacy_rows = connection.execute(select(legacy_table)).mappings().all()
    migrated = 0
    for legacy in legacy_rows:
        compat_config = build_schedule_trigger_config(
            instruction=legacy["instruction"],
            cron_expr=legacy["cron_expr"],
            delivery_target_json=legacy["delivery_target_json"],
            created_by=legacy["created_by"],
        )
        existing = connection.execute(
            select(trigger_table.c.id).where(trigger_table.c.id == legacy["id"])
        ).first()
        if existing:
            connection.execute(
                update(trigger_table)
                .where(trigger_table.c.id == legacy["id"])
                .values(
                    name=legacy["name"],
                    type="cron",
                    config=compat_config,
                    reason=legacy["instruction"],
                    is_enabled=legacy["is_enabled"],
                    last_fired_at=legacy["last_run_at"],
                    fire_count=legacy["run_count"] or 0,
                    created_at=legacy["created_at"],
                )
            )
        else:
            connection.execute(
                trigger_table.insert().values(
                    id=legacy["id"],
                    agent_id=legacy["agent_id"],
                    name=legacy["name"],
                    type="cron",
                    config=compat_config,
                    reason=legacy["instruction"],
                    is_enabled=legacy["is_enabled"],
                    last_fired_at=legacy["last_run_at"],
                    fire_count=legacy["run_count"] or 0,
                    cooldown_seconds=60,
                    created_at=legacy["created_at"],
                    focus_ref=None,
                    max_fires=None,
                    expires_at=None,
                    reply_context=None,
                )
            )
        connection.execute(delete(legacy_table).where(legacy_table.c.id == legacy["id"]))
        migrated += 1

    legacy_table.drop(bind=connection)
    return migrated
