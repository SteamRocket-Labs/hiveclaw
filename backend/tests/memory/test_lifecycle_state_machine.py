from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.memory.lifecycle_store import LifecycleStatus, MemoryLifecycleStore


def test_sketch_promotes_to_active_with_version_lineage() -> None:
    store = MemoryLifecycleStore()
    entry = store.create_sketch("Agent should draft vendor replies first.", expires_at=datetime(2026, 5, 23, tzinfo=UTC))

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

