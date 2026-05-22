"""Phase 13: access-count writeback tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.memory.access_log import bump_access
from app.memory.md_store import parse_entry_record


@pytest.fixture
def agent_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def memory_dir(tmp_path: Path, agent_id: uuid.UUID) -> Path:
    mem = tmp_path / str(agent_id) / "memory"
    mem.mkdir(parents=True)
    return mem


def _seed_entry(memory_dir: Path, filename: str, entry_id: str) -> Path:
    path = memory_dir / filename
    line = (
        f"- [2026-05-15 09:30][entry_id={entry_id}][sensitivity=PL1_public]"
        "[status=active][version=1][access_count=0][last_accessed=never] note about acme\n"
    )
    path.write_text(f"# Knowledge\n\n{line}", encoding="utf-8")
    return path


class TestBumpAccess:
    def test_increments_count_and_updates_timestamp(
        self, tmp_path: Path, agent_id: uuid.UUID, memory_dir: Path
    ) -> None:
        _seed_entry(memory_dir, "knowledge.md", "abc123")
        now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

        bumped = bump_access(
            tmp_path,
            agent_id,
            file_relpath="memory/knowledge.md",
            entry_id="abc123",
            now=now,
        )
        assert bumped is True

        text = (memory_dir / "knowledge.md").read_text(encoding="utf-8")
        line = [ln for ln in text.splitlines() if "entry_id=abc123" in ln][0]
        record = parse_entry_record(line)
        assert record.metadata["access_count"] == "1"
        assert record.metadata["last_accessed"].startswith("2026-05-22T12:00")

    def test_subsequent_bumps_accumulate(self, tmp_path: Path, agent_id: uuid.UUID, memory_dir: Path) -> None:
        _seed_entry(memory_dir, "knowledge.md", "abc123")
        now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
        bump_access(tmp_path, agent_id, file_relpath="memory/knowledge.md", entry_id="abc123", now=now)
        bump_access(tmp_path, agent_id, file_relpath="memory/knowledge.md", entry_id="abc123", now=now)
        bump_access(tmp_path, agent_id, file_relpath="memory/knowledge.md", entry_id="abc123", now=now)

        text = (memory_dir / "knowledge.md").read_text(encoding="utf-8")
        line = [ln for ln in text.splitlines() if "entry_id=abc123" in ln][0]
        record = parse_entry_record(line)
        assert record.metadata["access_count"] == "3"

    def test_unknown_entry_returns_false(self, tmp_path: Path, agent_id: uuid.UUID, memory_dir: Path) -> None:
        _seed_entry(memory_dir, "knowledge.md", "abc123")
        before = (memory_dir / "knowledge.md").read_text(encoding="utf-8")
        bumped = bump_access(
            tmp_path,
            agent_id,
            file_relpath="memory/knowledge.md",
            entry_id="does-not-exist",
        )
        assert bumped is False
        after = (memory_dir / "knowledge.md").read_text(encoding="utf-8")
        assert before == after

    def test_missing_file_returns_false(self, tmp_path: Path, agent_id: uuid.UUID, memory_dir: Path) -> None:
        bumped = bump_access(
            tmp_path,
            agent_id,
            file_relpath="memory/missing.md",
            entry_id="abc123",
        )
        assert bumped is False
