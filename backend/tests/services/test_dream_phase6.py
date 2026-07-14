"""Dream boundary tests after accepted-T3 writes moved to Platform Gate."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.auto_dream import (
    MIN_HEARTBEAT_TICKS_SINCE_DREAM,
    _T3_FILES,
    _consolidate_t3_files,
    _heartbeat_ticks_since_dream,
    _read_all_t3,
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
    (tmp_path / str(agent_id) / "memory" / "t3").mkdir(parents=True)
    (tmp_path / str(agent_id) / "memory" / "learnings").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_ticks(agent_id: uuid.UUID):
    yield
    _heartbeat_ticks_since_dream.pop(agent_id.hex, None)


class TestT3ReadWriteBoundary:
    def test_dream_t3_file_manifest_uses_two_plane_paths(self) -> None:
        assert "memory/self/self.md" in _T3_FILES
        assert "memory/profiles/owner.md" in _T3_FILES
        assert "memory/knowledge/<slug>.md" in _T3_FILES
        assert "memory/milestones/<slug>.md" in _T3_FILES
        assert all("memory/t3/" not in path and not path.startswith("t3/") for path in _T3_FILES)

    def test_reads_two_plane_t3_documents(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        memory_dir = tmp_agent_dir / str(agent_id) / "memory"
        (memory_dir / "self").mkdir(parents=True, exist_ok=True)
        (memory_dir / "knowledge").mkdir(parents=True, exist_ok=True)
        (memory_dir / "self" / "self.md").write_text(
            "## 方法\n\n### Verification discipline\n<!-- id: self-verification -->\nAlways run focused tests first.\n",
            encoding="utf-8",
        )
        (memory_dir / "knowledge" / "testing-policy.md").write_text(
            "---\ntitle: Testing Policy\nstatus: active\n---\n\n## Current Claim\nFocused tests precede full suite.\n",
            encoding="utf-8",
        )

        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            result = _read_all_t3(agent_id)

        assert "memory/self/self.md" in result
        assert "Always run focused tests first" in result["memory/self/self.md"]
        assert "memory/knowledge/testing-policy.md" in result
        assert "Focused tests precede full suite" in result["memory/knowledge/testing-policy.md"]

    def test_legacy_flat_t3_before_migration_is_observable_fallback(
        self, agent_id: uuid.UUID, tmp_agent_dir: Path
    ) -> None:
        memory_dir = tmp_agent_dir / str(agent_id) / "memory" / "t3"
        (memory_dir / "user.md").write_text("# T3 User\n- [2026-04-06] User prefers concise\n", encoding="utf-8")

        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            result = _read_all_t3(agent_id)

        assert "migration_required/memory/t3/user.md" in result
        assert "User prefers concise" in result["migration_required/memory/t3/user.md"]
        assert "legacy flat-T3 corpus" in result["migration_required/memory/t3/user.md"]

    def test_direct_t3_write_is_refused(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        with (
            patch("app.services.auto_dream.get_settings") as mock,
            pytest.raises(RuntimeError, match="direct T3 write refused"),
        ):
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            _write_t3_file(agent_id, "t3/user.md", "# T3 User\n- new entry\n")


class TestConsolidateT3:
    def test_mechanical_consolidation_is_noop(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        memory_dir = tmp_agent_dir / str(agent_id) / "memory" / "self"
        memory_dir.mkdir(parents=True, exist_ok=True)
        before = "## 方法\n\n### Concise output\n<!-- id: self-concise -->\nUser prefers concise output.\n"
        (memory_dir / "self.md").write_text(before, encoding="utf-8")

        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            stats = _consolidate_t3_files(agent_id)

        assert stats["memory/self/self.md"] == 0
        assert (memory_dir / "self.md").read_text(encoding="utf-8") == before


class TestUpdateIndexMd:
    def test_generates_wiki_map_from_canonical_t3(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        memory_dir = tmp_agent_dir / str(agent_id) / "memory"
        (memory_dir / "profiles").mkdir(parents=True, exist_ok=True)
        (memory_dir / "knowledge").mkdir(parents=True, exist_ok=True)
        (memory_dir / "profiles" / "owner.md").write_text(
            "## Owner Profile\n\n### User fact\n<!-- id: usr-fact -->\nUser prefers concise evidence.\n",
            encoding="utf-8",
        )
        (memory_dir / "knowledge" / "capability-pattern.md").write_text(
            "---\ntitle: Capability Pattern\nstatus: active\n---\n\n## Current Claim\nWeekly report package pattern.\n",
            encoding="utf-8",
        )

        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            _update_index_md(agent_id)

        index = (tmp_agent_dir / str(agent_id) / "memory" / "indexes" / "wiki_map.md").read_text(encoding="utf-8")
        assert "Memory Wiki Map" in index


class TestDreamGateExpansion:
    def test_heartbeat_ticks_constant(self) -> None:
        assert MIN_HEARTBEAT_TICKS_SINCE_DREAM == 2

    def test_record_heartbeat_tick(self, agent_id: uuid.UUID) -> None:
        record_heartbeat_tick(agent_id)
        assert _heartbeat_ticks_since_dream[agent_id.hex] == 1
        record_heartbeat_tick(agent_id)
        assert _heartbeat_ticks_since_dream[agent_id.hex] == 2

    def test_ticks_trigger_dream(self, agent_id: uuid.UUID) -> None:
        _heartbeat_ticks_since_dream[agent_id.hex] = 2
        with patch("app.services.auto_dream._load_dream_state", return_value=(None, 0)):
            assert should_dream(agent_id) is True


@pytest.mark.asyncio
async def test_run_dream_does_not_mechanically_rewrite_accepted_t3(
    agent_id: uuid.UUID,
    tmp_agent_dir: Path,
) -> None:
    memory_dir = tmp_agent_dir / str(agent_id) / "memory"
    t3_dir = memory_dir / "t3"
    before = "# T3 User\n- [2026-04-06] User prefers concise output\n- [2026-04-06] User prefers concise output\n"
    (t3_dir / "user.md").write_text(before, encoding="utf-8")
    (tmp_agent_dir / str(agent_id) / "soul.md").write_text("# Soul\n\n", encoding="utf-8")
    (memory_dir / "learnings" / "insights.md").write_text(
        "# Insights\n- [2026-04-01][status=absorbed][entry_id=t2-1] old\n",
        encoding="utf-8",
    )

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

    assert result["t3_deduped"] == 0
    assert (t3_dir / "user.md").read_text(encoding="utf-8") == before
    assert (memory_dir / "indexes" / "wiki_map.md").exists()
    assert not (memory_dir / "wiki_map.md").exists()


class TestDreamTemplate:
    def test_exists(self) -> None:
        from app.services.auto_dream import _DREAM_TEMPLATE_PATH

        assert _DREAM_TEMPLATE_PATH.exists()

    def test_has_new_soul_reconsolidation_contract(self) -> None:
        from app.services.auto_dream import _DREAM_TEMPLATE_PATH

        content = _DREAM_TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "Soul Reconsolidation Protocol" in content
        assert "You are not the T3 writer" in content
        assert "soul_candidate" in content
        assert "source_refs" in content
        assert "soul.md.next" in content
