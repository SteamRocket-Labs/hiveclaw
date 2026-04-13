from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4


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
