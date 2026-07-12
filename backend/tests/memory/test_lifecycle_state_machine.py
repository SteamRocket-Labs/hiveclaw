from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.memory import lifecycle_store as lifecycle_store_module
from app.memory.lifecycle_store import (
    LifecycleStatus,
    MemoryLifecycleStore,
    lifecycle_path,
    record_active_memory_lifecycle,
)


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
    reloaded = MemoryLifecycleStore(tmp_path / agent_id / "memory" / "control" / "lifecycle.json")
    assert reloaded.get("mem-1").content == "Alice prefers concise deployment summaries."
    assert reloaded.get("mem-1").metadata["sensitivity"] == "PL1_public"
    assert lifecycle_path(tmp_path, agent_id) == tmp_path / agent_id / "memory" / "control" / "lifecycle.json"
    assert not (tmp_path / agent_id / "memory" / "lifecycle.json").exists()


def test_read_lifecycle_metadata_supports_legacy_root_sidecar(tmp_path: Path) -> None:
    from app.memory.lifecycle_store import read_sidecar_metadata

    agent_id = "agent-legacy"
    legacy_path = tmp_path / agent_id / "memory" / "lifecycle.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        """[
  {
    "id": "mem-legacy",
    "content": "legacy",
    "status": "active",
    "version": 1,
    "parent_id": null,
    "supersedes": [],
    "superseded_by": null,
    "expires_at": null,
    "access_count": 0,
    "last_accessed": null,
    "metadata": {"source": "legacy"},
    "created_at": "2026-06-01T00:00:00+00:00",
    "updated_at": "2026-06-01T00:00:00+00:00"
  }
]""",
        encoding="utf-8",
    )

    assert read_sidecar_metadata(tmp_path, agent_id)["mem-legacy"]["source"] == "legacy"


def test_lifecycle_store_records_conflict_and_reference_revalidation(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.json"
    store = MemoryLifecycleStore(path)
    store.create_active(
        "Old deployment cadence is daily.",
        entry_id="mem-conflict",
        metadata={"source_refs": "workspace/old.md"},
    )

    store.record_conflict(
        "mem-conflict",
        conflicts_with=["mem-new"],
        reason="Newer owner instruction says twice daily.",
        source_refs=["workspace/new.md"],
        now=datetime(2026, 6, 15, tzinfo=UTC),
    )
    store.mark_reference_revalidation_required(
        "mem-conflict",
        reason="source file moved",
        source_refs=["workspace/old.md"],
        now=datetime(2026, 6, 15, tzinfo=UTC),
    )

    reloaded = MemoryLifecycleStore(path)
    entry = reloaded.get("mem-conflict")

    assert entry.metadata["conflict_status"] == "needs_review"
    assert entry.metadata["conflicts_with"] == "mem-new"
    assert entry.metadata["conflict_reason"] == "Newer owner instruction says twice daily."
    assert entry.metadata["conflict_source_refs"] == "workspace/new.md"
    assert entry.metadata["reference_status"] == "revalidation_required"
    assert entry.metadata["revalidation_reason"] == "source file moved"


def test_lifecycle_atomic_replace_failure_preserves_last_good_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lifecycle.json"
    store = MemoryLifecycleStore(path)
    store.create_active("durable", entry_id="mem-1")
    before = path.read_bytes()
    real_replace = lifecycle_store_module.os.replace

    def crash_before_primary_replace(source: str | Path, target: str | Path) -> None:
        if Path(target) == path:
            raise OSError("simulated process crash before primary replace")
        real_replace(source, target)

    monkeypatch.setattr(lifecycle_store_module.os, "replace", crash_before_primary_replace)

    with pytest.raises(OSError, match="simulated process crash"):
        store.bump_access("mem-1", now=datetime(2026, 7, 12, tzinfo=UTC))

    assert path.read_bytes() == before
    assert json.loads(path.read_text(encoding="utf-8"))[0]["access_count"] == 0
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_lifecycle_corruption_recovers_last_good_and_persists_evidence(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.json"
    store = MemoryLifecycleStore(path)
    store.create_active("durable", entry_id="mem-1")
    store.bump_access("mem-1", now=datetime(2026, 7, 12, tzinfo=UTC))
    corrupt_bytes = b'{"partial"'
    path.write_bytes(corrupt_bytes)

    recovered = MemoryLifecycleStore(path)

    assert recovered.get("mem-1").access_count == 1
    assert json.loads(path.read_text(encoding="utf-8"))[0]["id"] == "mem-1"
    recovery_root = tmp_path / "lifecycle-recovery"
    quarantined = list((recovery_root / "quarantine").glob("*.primary.corrupt"))
    receipts = list(recovery_root.glob("*.receipt.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == corrupt_bytes
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "hive.memory.lifecycle-recovery.v1"
    assert receipt["recovered_from_backup"] is True


def test_lifecycle_corruption_without_backup_is_quarantined_before_reinitialization(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.json"
    corrupt_bytes = b"not-json-and-must-survive"
    path.write_bytes(corrupt_bytes)

    store = MemoryLifecycleStore(path)

    assert store.entries() == []
    quarantined = list((tmp_path / "lifecycle-recovery" / "quarantine").glob("*.primary.corrupt"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == corrupt_bytes
    assert not path.exists()

    store.create_active("new generation", entry_id="mem-new")

    assert MemoryLifecycleStore(path).get("mem-new").content == "new generation"
    assert quarantined[0].read_bytes() == corrupt_bytes


def test_lifecycle_invalid_record_quarantines_the_whole_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.json"
    store = MemoryLifecycleStore(path)
    store.create_active("must not partially load", entry_id="mem-valid")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.append({"id": "missing-status"})
    path.with_name(f"{path.name}.last-good").unlink(missing_ok=True)
    malformed_snapshot = json.dumps(payload).encode("utf-8")
    path.write_bytes(malformed_snapshot)

    reloaded = MemoryLifecycleStore(path)

    assert reloaded.entries() == []
    quarantined = list((tmp_path / "lifecycle-recovery" / "quarantine").glob("*.primary.corrupt"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == malformed_snapshot


def test_lifecycle_mutation_reloads_latest_snapshot_before_write(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.json"
    seed = MemoryLifecycleStore(path)
    seed.create_active("shared", entry_id="mem-1")
    first = MemoryLifecycleStore(path)
    second = MemoryLifecycleStore(path)

    assert first.bump_access("mem-1", now=datetime(2026, 7, 12, 1, tzinfo=UTC))
    assert second.bump_access("mem-1", now=datetime(2026, 7, 12, 2, tzinfo=UTC))

    final = MemoryLifecycleStore(path).get("mem-1")
    assert final.access_count == 2
    assert final.last_accessed == datetime(2026, 7, 12, 2, tzinfo=UTC)


def test_lifecycle_supersede_commits_one_canonical_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "lifecycle.json"
    store = MemoryLifecycleStore(path)
    store.create_active("old", entry_id="mem-old")
    real_replace = lifecycle_store_module.os.replace
    primary_replaces = 0

    def count_primary_replaces(source: str | Path, target: str | Path) -> None:
        nonlocal primary_replaces
        if Path(target) == path:
            primary_replaces += 1
        real_replace(source, target)

    monkeypatch.setattr(lifecycle_store_module.os, "replace", count_primary_replaces)

    replacement = store.supersede("mem-old", "new")

    assert primary_replaces == 1
    reloaded = MemoryLifecycleStore(path)
    assert reloaded.get("mem-old").status == LifecycleStatus.SUPERSEDED
    assert reloaded.get("mem-old").superseded_by == replacement.id
    assert reloaded.get(replacement.id).parent_id == "mem-old"
