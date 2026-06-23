from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4


def test_memory_lifecycle_maintenance_discards_expired_sketches_and_reports_holds(tmp_path):
    from app.memory.lifecycle_maintenance import run_memory_lifecycle_maintenance
    from app.memory.lifecycle_store import LifecycleStatus, MemoryLifecycleStore, lifecycle_path

    agent_id = uuid4()
    now = datetime(2026, 6, 15, tzinfo=UTC)
    store = MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id))
    expired = store.create_sketch("stale assumption", entry_id="sketch-old", expires_at=now - timedelta(minutes=1))
    fresh = store.create_sketch("fresh assumption", entry_id="sketch-new", expires_at=now + timedelta(hours=1))
    store.create_active("old cadence", entry_id="mem-conflict")
    store.create_active("stale API reference", entry_id="mem-reference")
    store.record_conflict("mem-conflict", conflicts_with=["mem-new"], reason="newer cadence", now=now)
    store.mark_reference_revalidation_required(
        "mem-reference",
        reason="source moved",
        source_refs=["workspace/old.md"],
        now=now,
    )

    report = run_memory_lifecycle_maintenance(tmp_path, agent_id, now=now)

    reloaded = MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id))
    assert reloaded.get(expired.id).status == LifecycleStatus.DISCARDED
    assert reloaded.get(fresh.id).status == LifecycleStatus.SKETCH
    assert report["discarded_expired"] == ["sketch-old"]
    assert report["conflict_holds"] == ["mem-conflict"]
    assert report["revalidation_holds"] == ["mem-reference"]

    artifact = tmp_path / str(agent_id) / "memory" / "control" / "lifecycle_maintenance.json"
    assert json.loads(artifact.read_text(encoding="utf-8"))["schema"] == "memory_lifecycle_maintenance.v1"
    assert not (tmp_path / str(agent_id) / "memory" / "lifecycle_maintenance.json").exists()
