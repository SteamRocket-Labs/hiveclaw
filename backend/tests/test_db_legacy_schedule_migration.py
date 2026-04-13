from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, create_engine, inspect, select
from sqlalchemy.types import JSON


def _create_legacy_tables(metadata: MetaData) -> tuple[Table, Table]:
    schedules = Table(
        "agent_schedules",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("name", String(200), nullable=False),
        Column("instruction", String(), nullable=False),
        Column("cron_expr", String(100), nullable=False),
        Column("is_enabled", Boolean(), nullable=False),
        Column("last_run_at", DateTime(timezone=True)),
        Column("run_count", Integer()),
        Column("created_by", String(36), nullable=True),
        Column("delivery_target_json", JSON, nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    triggers = Table(
        "agent_triggers",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("name", String(100), nullable=False),
        Column("type", String(20), nullable=False),
        Column("config", JSON, nullable=False),
        Column("reason", String(), nullable=False),
        Column("focus_ref", String(200), nullable=True),
        Column("is_enabled", Boolean(), nullable=False),
        Column("last_fired_at", DateTime(timezone=True), nullable=True),
        Column("fire_count", Integer(), nullable=False),
        Column("max_fires", Integer(), nullable=True),
        Column("cooldown_seconds", Integer(), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=True),
        Column("reply_context", JSON, nullable=True),
    )
    return schedules, triggers


def test_promote_legacy_schedules_to_triggers_moves_rows_and_drops_legacy_table() -> None:
    from app.db_legacy_schedule_migration import promote_legacy_schedules_to_triggers

    metadata = MetaData()
    schedules, triggers = _create_legacy_tables(metadata)
    engine = create_engine("sqlite:///:memory:")

    created_at = datetime(2026, 4, 14, 8, 0, tzinfo=timezone.utc)
    last_run_at = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

    with engine.begin() as conn:
        metadata.create_all(conn)
        conn.execute(
            schedules.insert().values(
                id="schedule-1",
                agent_id="agent-1",
                name="日报",
                instruction="生成日报",
                cron_expr="0 9 * * *",
                is_enabled=True,
                last_run_at=last_run_at,
                run_count=2,
                created_by="user-1",
                delivery_target_json={"channel": "feishu"},
                created_at=created_at,
            )
        )

        migrated = promote_legacy_schedules_to_triggers(conn)

        remaining_tables = set(inspect(conn).get_table_names())
        trigger_rows = conn.execute(select(triggers)).mappings().all()

    assert migrated == 1
    assert "agent_schedules" not in remaining_tables
    assert len(trigger_rows) == 1
    row = trigger_rows[0]
    assert row["id"] == "schedule-1"
    assert row["agent_id"] == "agent-1"
    assert row["type"] == "cron"
    assert row["reason"] == "生成日报"
    assert row["fire_count"] == 2
    assert row["last_fired_at"].replace(tzinfo=timezone.utc) == last_run_at
    assert row["config"]["_surface"] == "schedule_api"
    assert row["config"]["expr"] == "0 9 * * *"
    assert row["config"]["instruction"] == "生成日报"
    assert row["config"]["created_by"] == "user-1"
    assert row["config"]["delivery_target_json"] == {"channel": "feishu"}


def test_promote_legacy_schedules_to_triggers_noops_without_legacy_table() -> None:
    from app.db_legacy_schedule_migration import promote_legacy_schedules_to_triggers

    metadata = MetaData()
    _create_legacy_tables(metadata)
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        metadata.create_all(conn, tables=[metadata.tables["agent_triggers"]])

        migrated = promote_legacy_schedules_to_triggers(conn)

        remaining_tables = set(inspect(conn).get_table_names())

    assert migrated == 0
    assert remaining_tables == {"agent_triggers"}
