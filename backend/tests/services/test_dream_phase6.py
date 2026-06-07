"""Tests for Phase 6 dream MD→MD consolidation."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.auto_dream import (
    MIN_HEARTBEAT_TICKS_SINCE_DREAM,
    _consolidate_t3_files,
    _heartbeat_ticks_since_dream,
    _programmatic_dedup,
    _read_all_t3,
    _truncate_t2,
    _update_index_md,
    _write_t3_file,
    record_heartbeat_tick,
    run_dream,
    should_dream,
)


@pytest.fixture
def agent_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def tmp_agent_dir(tmp_path: Path, agent_id: uuid.UUID) -> Path:
    """Create temp agent data dir with memory/ and learnings/."""
    agent_dir = tmp_path / str(agent_id)
    (agent_dir / "memory" / "learnings").mkdir(parents=True)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_ticks(agent_id: uuid.UUID):
    yield
    _heartbeat_ticks_since_dream.pop(agent_id.hex, None)


# ── _programmatic_dedup ──


class TestProgrammaticDedup:
    def test_removes_exact_duplicates(self) -> None:
        lines = ["- [2026-04-06] User prefers concise", "- [2026-04-06] User prefers concise"]
        result = _programmatic_dedup(lines)
        assert len(result) == 1

    def test_removes_near_duplicates(self) -> None:
        lines = [
            "- [2026-04-06] User prefers concise output",
            "- [2026-04-05] User prefers concise outputs",
        ]
        result = _programmatic_dedup(lines, similarity_threshold=0.7)
        assert len(result) == 1

    def test_keeps_distinct(self) -> None:
        lines = [
            "- [2026-04-06] User prefers snake_case",
            "- [2026-04-06] Project uses PostgreSQL 15",
        ]
        result = _programmatic_dedup(lines)
        assert len(result) == 2

    def test_empty(self) -> None:
        assert _programmatic_dedup([]) == []

    def test_single(self) -> None:
        assert _programmatic_dedup(["one"]) == ["one"]


# ── _read_all_t3 / _write_t3_file ──


class TestT3ReadWrite:
    def test_reads_existing_files(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        memory_dir = tmp_agent_dir / str(agent_id) / "memory"
        (memory_dir / "feedback.md").write_text("# Feedback\n- [2026-04-06] test\n")

        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            result = _read_all_t3(agent_id)

        assert "feedback.md" in result
        assert "test" in result["feedback.md"]

    def test_writes_file(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            _write_t3_file(agent_id, "feedback.md", "# Feedback\n- new entry\n")

        fpath = tmp_agent_dir / str(agent_id) / "memory" / "feedback.md"
        assert "new entry" in fpath.read_text()


# ── _consolidate_t3_files ──


class TestConsolidateT3:
    def test_dedup_and_cap(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        memory_dir = tmp_agent_dir / str(agent_id) / "memory"
        # Create file with duplicates
        entries = ["- [2026-04-06] User prefers concise output"] * 5 + [
            "- [2026-04-06] Project uses PostgreSQL",
            "- [2026-04-05] API key is in .env",
        ]
        (memory_dir / "feedback.md").write_text("# Feedback\n" + "\n".join(entries) + "\n")

        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            stats = _consolidate_t3_files(agent_id)

        assert stats["feedback.md"] > 0  # Some duplicates removed
        content = (memory_dir / "feedback.md").read_text()
        assert content.count("User prefers concise") == 1

    def test_no_changes_when_clean(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        memory_dir = tmp_agent_dir / str(agent_id) / "memory"
        (memory_dir / "feedback.md").write_text("# Feedback\n- [2026-04-06] unique entry\n")

        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            stats = _consolidate_t3_files(agent_id)

        assert stats.get("feedback.md", 0) == 0


# ── _truncate_t2 ──


class TestTruncateT2:
    def test_archives_absorbed_entries_to_keep(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        learnings = tmp_agent_dir / str(agent_id) / "memory" / "learnings"
        entries = [f"- [2026-04-{i:02d}][status=absorbed][entry_id=t2-{i}] entry {i}" for i in range(1, 21)]
        (learnings / "insights.md").write_text("# Insights\n" + "\n".join(entries) + "\n")

        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            removed = _truncate_t2(agent_id, keep=5)

        assert removed == 15
        content = (learnings / "insights.md").read_text()
        archive = (tmp_agent_dir / str(agent_id) / "memory" / "archive.md").read_text()
        assert "entry 20" in content  # Most recent kept
        assert "- [2026-04-01] entry 1\n" not in content  # Oldest removed
        assert "entry 1" in archive
        assert "t2_retention_cap" in archive

    def test_active_entries_are_never_archived_by_cap(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        learnings = tmp_agent_dir / str(agent_id) / "memory" / "learnings"
        entries = [f"- [2026-04-{i:02d}][status=active][entry_id=t2-{i}] entry {i}" for i in range(1, 21)]
        (learnings / "insights.md").write_text("# Insights\n" + "\n".join(entries) + "\n")

        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            removed = _truncate_t2(agent_id, keep=5)

        assert removed == 0
        content = (learnings / "insights.md").read_text()
        assert "entry 1" in content
        assert not (tmp_agent_dir / str(agent_id) / "memory" / "archive.md").exists()

    def test_noop_when_under_cap(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        learnings = tmp_agent_dir / str(agent_id) / "memory" / "learnings"
        (learnings / "insights.md").write_text("# Insights\n- [2026-04-06] one\n- [2026-04-06] two\n")

        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            removed = _truncate_t2(agent_id, keep=10)

        assert removed == 0


# ── _update_index_md ──


class TestUpdateIndexMd:
    def test_generates_index(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        memory_dir = tmp_agent_dir / str(agent_id) / "memory"
        (memory_dir / "feedback.md").write_text("# Feedback\n- [2026-04-06] a\n- [2026-04-06] b\n")
        (memory_dir / "knowledge.md").write_text("# Knowledge\n- [2026-04-06] x\n")

        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            _update_index_md(agent_id)

        index = (memory_dir / "INDEX.md").read_text()
        assert "feedback.md" in index
        assert "| feedback.md |" in index
        assert "| feedback, constraint | 2 |" in index
        assert "knowledge.md" in index
        assert "| knowledge.md |" in index
        assert "| project, reference, general | 1 |" in index


# ── Dream gate expansion ──


class TestDreamGateExpansion:
    def test_heartbeat_ticks_constant(self) -> None:
        assert MIN_HEARTBEAT_TICKS_SINCE_DREAM == 2

    def test_record_heartbeat_tick(self, agent_id: uuid.UUID) -> None:
        record_heartbeat_tick(agent_id)
        assert _heartbeat_ticks_since_dream[agent_id.hex] == 1
        record_heartbeat_tick(agent_id)
        assert _heartbeat_ticks_since_dream[agent_id.hex] == 2

    def test_ticks_trigger_dream(self, agent_id: uuid.UUID) -> None:
        """2 heartbeat ticks should trigger dream even with 0 sessions."""
        _heartbeat_ticks_since_dream[agent_id.hex] = 2
        # No sessions, no prior dream → should_dream checks ticks
        with patch("app.services.auto_dream._load_dream_state", return_value=(None, 0)):
            result = should_dream(agent_id)
        assert result is True

    def test_insufficient_ticks(self, agent_id: uuid.UUID) -> None:
        _heartbeat_ticks_since_dream[agent_id.hex] = 1
        with patch("app.services.auto_dream._load_dream_state", return_value=(None, 0)):
            result = should_dream(agent_id)
        assert result is False


@pytest.mark.asyncio
async def test_run_dream_consolidates_md_files_without_preexisting_semantic_store(
    agent_id: uuid.UUID,
    tmp_agent_dir: Path,
) -> None:
    memory_dir = tmp_agent_dir / str(agent_id) / "memory"
    learnings_dir = memory_dir / "learnings"
    memory_dir.mkdir(parents=True, exist_ok=True)
    learnings_dir.mkdir(parents=True, exist_ok=True)

    repeated_feedback = [
        "- [2026-04-06] User prefers concise output",
        "- [2026-04-06] User prefers concise output",
        "- [2026-04-05] User prefers concise outputs",
    ]
    (memory_dir / "feedback.md").write_text("# Feedback\n" + "\n".join(repeated_feedback) + "\n", encoding="utf-8")
    (memory_dir / "knowledge.md").write_text(
        "# Knowledge\n- [2026-04-06] Project uses FastAPI\n",
        encoding="utf-8",
    )
    (memory_dir / "user.md").write_text("# User Profile\n", encoding="utf-8")
    (tmp_agent_dir / str(agent_id) / "soul.md").write_text("# Soul\n\n", encoding="utf-8")
    t2_entries = [f"- [2026-04-{i:02d}] entry {i}" for i in range(1, 16)]
    (learnings_dir / "insights.md").write_text("# Insights\n" + "\n".join(t2_entries) + "\n", encoding="utf-8")

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    with (
        patch("app.services.auto_dream.get_settings") as mock_settings,
        patch("app.services.t0_logger.get_settings") as mock_t0_settings,
        patch("app.runtime.hooks.emit_hook", fake_emit_hook),
    ):
        mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
        mock_t0_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
        result = await run_dream(agent_id, uuid.uuid4())

    feedback_content = (memory_dir / "feedback.md").read_text(encoding="utf-8")
    soul_content = (tmp_agent_dir / str(agent_id) / "soul.md").read_text(encoding="utf-8")
    index_content = (memory_dir / "INDEX.md").read_text(encoding="utf-8")
    truncated_t2 = (learnings_dir / "insights.md").read_text(encoding="utf-8")

    assert result["consolidated"] >= 2
    assert result["t3_deduped"] >= 1
    assert feedback_content.count("User prefers concise") == 1
    assert "User prefers concise" in soul_content
    assert "feedback.md" in index_content
    assert "entry 15" in truncated_t2
    assert "- [2026-04-01] entry 1\n" not in truncated_t2


# ── DREAM.md template ──


class TestDreamTemplate:
    def test_exists(self) -> None:
        from app.services.auto_dream import _DREAM_TEMPLATE_PATH
        assert _DREAM_TEMPLATE_PATH.exists()

    def test_has_4_phases(self) -> None:
        from app.services.auto_dream import _DREAM_TEMPLATE_PATH
        content = _DREAM_TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "## Phase 1: ORIENT" in content
        assert "## Phase 2: CONSOLIDATE" in content
        assert "## Phase 3: PROMOTE" in content
        assert "## Phase 4: INDEX + CLEANUP" in content

    def test_has_soul_promotion(self) -> None:
        from app.services.auto_dream import _DREAM_TEMPLATE_PATH
        content = _DREAM_TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "soul.md" in content
        assert "Learned Behaviors" in content

    def test_has_dream_prefix(self) -> None:
        from app.services.auto_dream import _DREAM_TEMPLATE_PATH
        content = _DREAM_TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "DREAM-" in content
