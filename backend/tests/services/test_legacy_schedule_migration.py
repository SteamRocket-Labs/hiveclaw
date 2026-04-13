from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _MappingsResult:
    def __init__(self, values):
        self._values = list(values)

    def mappings(self):
        return self

    def all(self):
        return list(self._values)


class _FakeDB:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.added = []
        self.deleted = []

    async def execute(self, _stmt):
        if getattr(_stmt, "is_delete", False):
            self.deleted.append(_stmt)
            return None
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)


def _legacy_schedule_table():
    metadata = MetaData()
    return Table(
        "agent_schedules",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("agent_id", UUID(as_uuid=True), nullable=False),
        Column("name", String(200), nullable=False),
        Column("instruction", Text(), nullable=False),
        Column("cron_expr", String(100), nullable=False),
        Column("is_enabled", Boolean(), nullable=False),
        Column("last_run_at", DateTime(timezone=True)),
        Column("run_count", Integer()),
        Column("created_by", UUID(as_uuid=True), nullable=False),
        Column("delivery_target_json", JSONB),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )


def test_schedule_response_payload_reads_trigger_compat_fields():
    from app.services.schedule_surface import SCHEDULE_TRIGGER_SURFACE, schedule_response_payload

    created_by = uuid4()
    created_at = datetime(2026, 4, 14, 1, 0, tzinfo=timezone.utc)
    last_run_at = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="日报",
        type="cron",
        config={
            "_surface": SCHEDULE_TRIGGER_SURFACE,
            "expr": "0 9 * * *",
            "instruction": "生成日报",
            "delivery_target_json": {"channel": "feishu"},
            "created_by": str(created_by),
        },
        reason="生成日报",
        is_enabled=True,
        last_fired_at=last_run_at,
        fire_count=3,
        created_at=created_at,
    )

    payload = schedule_response_payload(trigger, creator_username="alice")

    assert payload["id"] == trigger.id
    assert payload["instruction"] == "生成日报"
    assert payload["cron_expr"] == "0 9 * * *"
    assert payload["run_count"] == 3
    assert payload["created_by"] == created_by
    assert payload["creator_username"] == "alice"
    assert payload["delivery_target_json"] == {"channel": "feishu"}
    assert payload["last_run_at"] == last_run_at
    assert payload["next_run_at"] is not None


def test_mark_schedule_manual_pending_sets_request_marker():
    from app.services.schedule_surface import mark_schedule_manual_pending

    config = {"expr": "0 9 * * *"}
    updated = mark_schedule_manual_pending(config)

    assert updated["_manual_pending"] is True
    assert isinstance(updated["_manual_request_id"], str)
    assert updated["_manual_request_id"]
    assert updated["expr"] == "0 9 * * *"


def test_build_schedule_activity_entries_only_emits_schedule_surface_triggers():
    from app.services.schedule_surface import (
        SCHEDULE_TRIGGER_SURFACE,
        build_schedule_activity_entries,
    )

    compat_trigger = SimpleNamespace(
        id=uuid4(),
        name="日报",
        type="cron",
        config={
            "_surface": SCHEDULE_TRIGGER_SURFACE,
            "instruction": "生成日报",
        },
    )
    raw_trigger = SimpleNamespace(
        id=uuid4(),
        name="普通 cron",
        config={"expr": "0 9 * * *"},
    )

    entries = build_schedule_activity_entries([compat_trigger, raw_trigger], "已完成")

    assert len(entries) == 1
    assert entries[0]["action_type"] == "schedule_run"
    assert entries[0]["detail"]["schedule_id"] == str(compat_trigger.id)
    assert entries[0]["detail"]["instruction"] == "生成日报"
    assert entries[0]["detail"]["reply"] == "已完成"


@pytest.mark.asyncio
async def test_migrate_legacy_schedules_creates_schedule_surface_triggers():
    from app.services import legacy_schedule_migration as compat_module
    from app.services.schedule_surface import SCHEDULE_TRIGGER_SURFACE

    schedule_id = uuid4()
    agent_id = uuid4()
    created_by = uuid4()
    legacy_schedule = {
        "id": schedule_id,
        "agent_id": agent_id,
        "name": "日报",
        "instruction": "生成日报",
        "cron_expr": "0 9 * * *",
        "is_enabled": True,
        "run_count": 2,
        "last_run_at": datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc),
        "delivery_target_json": {"channel": "feishu"},
        "created_by": created_by,
        "created_at": datetime(2026, 4, 13, 1, 0, tzinfo=timezone.utc),
    }
    legacy_table = _legacy_schedule_table()
    db = _FakeDB([
        _MappingsResult([legacy_schedule]),
        _ScalarsResult([]),
    ])

    migrated = await compat_module.migrate_legacy_schedules(db, agent_id, legacy_table=legacy_table)

    assert migrated == 1
    assert len(db.deleted) == 1
    assert len(db.added) == 1
    trigger = db.added[0]
    assert trigger.id == schedule_id
    assert trigger.agent_id == agent_id
    assert trigger.type == "cron"
    assert trigger.reason == "生成日报"
    assert trigger.fire_count == 2
    assert trigger.last_fired_at == legacy_schedule["last_run_at"]
    assert trigger.config["_surface"] == SCHEDULE_TRIGGER_SURFACE
    assert trigger.config["expr"] == "0 9 * * *"
    assert trigger.config["instruction"] == "生成日报"
    assert trigger.config["created_by"] == str(created_by)
    assert trigger.config["delivery_target_json"] == {"channel": "feishu"}


@pytest.mark.asyncio
async def test_migrate_all_legacy_schedules_batches_by_agent(monkeypatch):
    from app.services import legacy_schedule_migration as compat_module

    agent_a = uuid4()
    agent_b = uuid4()
    calls: list[uuid.UUID] = []

    class _DistinctResult:
        def __init__(self, values):
            self._values = values

        def scalars(self):
            return self

        def all(self):
            return list(self._values)

    class _FakeSessionCtx:
        def __init__(self):
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _stmt):
            return _DistinctResult([agent_a, agent_b])

        async def commit(self):
            self.committed = True

    legacy_table = _legacy_schedule_table()

    async def fake_migrate_legacy_schedules(db, agent_id, *, legacy_table=None):
        assert db is not None
        assert legacy_table is not None
        calls.append(agent_id)
        return 1 if agent_id == agent_a else 2

    async def fake_load_legacy_schedule_table(_db):
        return legacy_table

    session_ctx = _FakeSessionCtx()
    monkeypatch.setattr(compat_module, "async_session", lambda: session_ctx)
    monkeypatch.setattr(compat_module, "_load_legacy_schedule_table", fake_load_legacy_schedule_table)
    monkeypatch.setattr(compat_module, "migrate_legacy_schedules", fake_migrate_legacy_schedules)

    migrated = await compat_module.migrate_all_legacy_schedules()

    assert migrated == 3
    assert calls == [agent_a, agent_b]
    assert session_ctx.committed is True


@pytest.mark.asyncio
async def test_migrate_all_legacy_schedules_noops_when_legacy_table_absent(monkeypatch):
    from app.services import legacy_schedule_migration as compat_module

    class _FakeSessionCtx:
        def __init__(self):
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _stmt):
            raise AssertionError("execute() should not be called when legacy table is absent")

        async def commit(self):
            self.committed = True

    session_ctx = _FakeSessionCtx()

    async def fake_table_exists(_db):
        return False

    monkeypatch.setattr(compat_module, "async_session", lambda: session_ctx)
    monkeypatch.setattr(compat_module, "_legacy_schedule_table_exists", fake_table_exists)

    migrated = await compat_module.migrate_all_legacy_schedules()

    assert migrated == 0
    assert session_ctx.committed is False
