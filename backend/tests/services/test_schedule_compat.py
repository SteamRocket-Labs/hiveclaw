from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _FakeDB:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.added = []
        self.deleted = []

    async def execute(self, _stmt):
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)


def test_schedule_response_payload_reads_trigger_compat_fields():
    from app.services.schedule_compat import SCHEDULE_TRIGGER_SURFACE, schedule_response_payload

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
    from app.services.schedule_compat import mark_schedule_manual_pending

    config = {"expr": "0 9 * * *"}
    updated = mark_schedule_manual_pending(config)

    assert updated["_manual_pending"] is True
    assert isinstance(updated["_manual_request_id"], str)
    assert updated["_manual_request_id"]
    assert updated["expr"] == "0 9 * * *"


def test_build_schedule_activity_entries_only_emits_schedule_surface_triggers():
    from app.services.schedule_compat import (
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
    from app.services.schedule_compat import SCHEDULE_TRIGGER_SURFACE, migrate_legacy_schedules

    schedule_id = uuid4()
    agent_id = uuid4()
    created_by = uuid4()
    legacy_schedule = SimpleNamespace(
        id=schedule_id,
        agent_id=agent_id,
        name="日报",
        instruction="生成日报",
        cron_expr="0 9 * * *",
        is_enabled=True,
        run_count=2,
        last_run_at=datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc),
        delivery_target_json={"channel": "feishu"},
        created_by=created_by,
        created_at=datetime(2026, 4, 13, 1, 0, tzinfo=timezone.utc),
    )
    db = _FakeDB([
        _ScalarsResult([legacy_schedule]),
        _ScalarsResult([]),
    ])

    migrated = await migrate_legacy_schedules(db, agent_id)

    assert migrated == 1
    assert db.deleted == [legacy_schedule]
    assert len(db.added) == 1
    trigger = db.added[0]
    assert trigger.id == schedule_id
    assert trigger.agent_id == agent_id
    assert trigger.type == "cron"
    assert trigger.reason == "生成日报"
    assert trigger.fire_count == 2
    assert trigger.last_fired_at == legacy_schedule.last_run_at
    assert trigger.config["_surface"] == SCHEDULE_TRIGGER_SURFACE
    assert trigger.config["expr"] == "0 9 * * *"
    assert trigger.config["instruction"] == "生成日报"
    assert trigger.config["created_by"] == str(created_by)
    assert trigger.config["delivery_target_json"] == {"channel": "feishu"}
