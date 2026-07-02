"""D1 (purity debt): access telemetry lives in the lifecycle sidecar, never
rewritten into `.md` prose.

Up to Phase 13 `bump_access` used a regex to stamp `[access_count=N]` and
`[last_accessed=...]` back into the markdown line on every prompt activation —
the canonical "telemetry stamped into prose" pollution (purity-debt D1). The
sidecar (`lifecycle.json`) already carries an `access_count`/`last_accessed`
field per entry; D1 moves the writeback there and leaves the prose untouched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.memory.access_log import bump_access
from app.memory.lifecycle_store import (
    MemoryLifecycleStore,
    lifecycle_path,
    record_active_memory_lifecycle,
)


@pytest.fixture
def agent_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def memory_dir(tmp_path: Path, agent_id: uuid.UUID) -> Path:
    mem = tmp_path / str(agent_id) / "memory"
    mem.mkdir(parents=True)
    return mem


def _seed_entry(tmp_path: Path, agent_id: uuid.UUID, memory_dir: Path, filename: str, entry_id: str) -> Path:
    """Seed a clean-prose `.md` line (no inline telemetry) plus the sidecar record.

    Mirrors the D1 target state: prose carries only the date + entry_id join
    key + governance metadata; telemetry (access_count/last_accessed) lives in
    `lifecycle.json`, created on write by `record_active_memory_lifecycle`.
    """
    path = memory_dir / filename
    line = (
        f"- [2026-05-15 09:30][entry_id={entry_id}][sensitivity=PL1_public][status=active][version=1] note about acme\n"
    )
    path.write_text(f"# Knowledge\n\n{line}", encoding="utf-8")
    record_active_memory_lifecycle(
        tmp_path,
        agent_id,
        content="note about acme",
        metadata={
            "entry_id": entry_id,
            "sensitivity": "PL1_public",
            "status": "active",
            "version": "1",
            "access_count": "0",
            "last_accessed": "never",
        },
    )
    return path


def _sidecar_entry(tmp_path: Path, agent_id: uuid.UUID, entry_id: str):
    store = MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id))
    return store.get(entry_id)


class TestBumpAccessWritesSidecarNotProse:
    def test_bump_increments_sidecar_count(self, tmp_path: Path, agent_id: uuid.UUID, memory_dir: Path) -> None:
        _seed_entry(tmp_path, agent_id, memory_dir, "knowledge.md", "abc123")
        now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

        bumped = bump_access(
            tmp_path,
            agent_id,
            file_relpath="memory/knowledge.md",
            entry_id="abc123",
            now=now,
        )

        assert bumped is True
        entry = _sidecar_entry(tmp_path, agent_id, "abc123")
        assert entry.access_count == 1
        assert entry.last_accessed is not None
        assert entry.last_accessed.isoformat().startswith("2026-05-22T12:00")

    def test_bump_does_not_touch_md_prose(self, tmp_path: Path, agent_id: uuid.UUID, memory_dir: Path) -> None:
        path = _seed_entry(tmp_path, agent_id, memory_dir, "knowledge.md", "abc123")
        before = path.read_text(encoding="utf-8")

        bump_access(
            tmp_path,
            agent_id,
            file_relpath="memory/knowledge.md",
            entry_id="abc123",
            now=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        )

        after = path.read_text(encoding="utf-8")
        assert after == before, "telemetry must never be stamped back into .md prose"
        assert "access_count" not in after
        assert "last_accessed" not in after

    def test_subsequent_bumps_accumulate_in_sidecar(
        self, tmp_path: Path, agent_id: uuid.UUID, memory_dir: Path
    ) -> None:
        path = _seed_entry(tmp_path, agent_id, memory_dir, "knowledge.md", "abc123")
        before = path.read_text(encoding="utf-8")
        for _ in range(3):
            bump_access(tmp_path, agent_id, file_relpath="memory/knowledge.md", entry_id="abc123")

        assert _sidecar_entry(tmp_path, agent_id, "abc123").access_count == 3
        assert path.read_text(encoding="utf-8") == before

    def test_unknown_entry_returns_false(self, tmp_path: Path, agent_id: uuid.UUID, memory_dir: Path) -> None:
        _seed_entry(tmp_path, agent_id, memory_dir, "knowledge.md", "abc123")
        bumped = bump_access(
            tmp_path,
            agent_id,
            file_relpath="memory/knowledge.md",
            entry_id="does-not-exist",
        )
        assert bumped is False

    def test_missing_sidecar_returns_false(self, tmp_path: Path, agent_id: uuid.UUID, memory_dir: Path) -> None:
        # Prose line exists but no sidecar record was ever created for it.
        (memory_dir / "knowledge.md").write_text(
            "# Knowledge\n\n- [2026-05-15][entry_id=orphan] note\n", encoding="utf-8"
        )
        bumped = bump_access(
            tmp_path,
            agent_id,
            file_relpath="memory/knowledge.md",
            entry_id="orphan",
        )
        assert bumped is False
