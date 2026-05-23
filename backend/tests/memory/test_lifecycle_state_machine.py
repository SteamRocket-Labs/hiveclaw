from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.memory.lifecycle_store import LifecycleStatus, MemoryLifecycleStore, record_active_memory_lifecycle


def test_sketch_promotes_to_active_with_version_lineage() -> None:
    store = MemoryLifecycleStore()
    entry = store.create_sketch(
        "Agent should draft vendor replies first.", expires_at=datetime(2026, 5, 23, tzinfo=UTC)
    )

    promoted = store.promote(entry.id, approved_by="owner")

    assert promoted.status == LifecycleStatus.ACTIVE
    assert promoted.version == 1
    assert promoted.metadata["approved_by"] == "owner"


def test_supersede_keeps_old_entry_and_creates_new_version() -> None:
    store = MemoryLifecycleStore()
    original = store.create_active("Owner prefers concise replies.")

    replacement = store.supersede(original.id, "Owner prefers concise replies with evidence first.")

    assert store.get(original.id).status == LifecycleStatus.SUPERSEDED
    assert store.get(original.id).superseded_by == replacement.id
    assert replacement.parent_id == original.id
    assert replacement.version == 2


def test_expired_sketch_is_discarded_not_promoted() -> None:
    now = datetime(2026, 5, 22, tzinfo=UTC)
    store = MemoryLifecycleStore()
    sketch = store.create_sketch("Unverified assumption", expires_at=now - timedelta(minutes=1))

    discarded = store.discard_expired(now=now)

    assert discarded == [sketch.id]
    assert store.get(sketch.id).status == LifecycleStatus.DISCARDED


def test_lifecycle_store_persists_state_machine(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.json"
    store = MemoryLifecycleStore(path)
    entry = store.create_sketch(
        "Owner prefers evidence-first summaries.",
        expires_at=datetime(2026, 5, 23, tzinfo=UTC),
        entry_id="entry-1",
        metadata={"source": "test"},
    )
    store.promote(entry.id, approved_by="owner")

    reloaded = MemoryLifecycleStore(path)
    loaded = reloaded.get("entry-1")

    assert loaded.status == LifecycleStatus.ACTIVE
    assert loaded.metadata["approved_by"] == "owner"
    assert loaded.metadata["source"] == "test"

    replacement = reloaded.supersede("entry-1", "Owner prefers evidence-first summaries with citations.")
    final = MemoryLifecycleStore(path)

    assert final.get("entry-1").status == LifecycleStatus.SUPERSEDED
    assert final.get("entry-1").superseded_by == replacement.id
    assert final.get(replacement.id).parent_id == "entry-1"


def test_record_active_memory_lifecycle_uses_memory_entry_id(tmp_path: Path) -> None:
    agent_id = "agent-1"

    entry = record_active_memory_lifecycle(
        tmp_path,
        agent_id,
        content="Alice prefers concise deployment summaries.",
        metadata={"entry_id": "mem-1", "sensitivity": "PL1_public", "status": "active"},
    )

    assert entry.id == "mem-1"
    reloaded = MemoryLifecycleStore(tmp_path / agent_id / "memory" / "lifecycle.json")
    assert reloaded.get("mem-1").content == "Alice prefers concise deployment summaries."
    assert reloaded.get("mem-1").metadata["sensitivity"] == "PL1_public"
