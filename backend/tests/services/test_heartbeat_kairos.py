"""Heartbeat direct-core compatibility tests.

The old KAIROS persistent heartbeat session was retired. Heartbeat now keeps
only maintenance caches and delegates semantic curation to the direct T3 core.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.heartbeat import (
    _heartbeat_tick_counts,
    _read_incremental_t2,
    _read_pending_t3_intake,
    _read_t2_full,
    _read_t3_summary,
    _reset_heartbeat_session,
    _t2_mtimes,
)


@pytest.fixture
def agent_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture(autouse=True)
def _clean_state(agent_id: uuid.UUID):
    yield
    _heartbeat_tick_counts.pop(agent_id, None)
    _t2_mtimes.pop(agent_id, None)


@pytest.fixture
def tmp_agent_dir(tmp_path: Path, agent_id: uuid.UUID) -> Path:
    agent_dir = tmp_path / str(agent_id)
    (agent_dir / "memory" / "learnings").mkdir(parents=True)
    return tmp_path


def _write_t2_package(
    root: Path,
    agent_id: uuid.UUID,
    *,
    session_id: str = "s1",
    segment_id: str = "seg-1",
    summary: str | None = None,
    labels: str | None = None,
    review: str = "<t2_review><decision>approved</decision><allowed_next>t3_intake</allowed_next></t2_review>",
    source_refs: list[str] | None = None,
) -> Path:
    package_dir = root / str(agent_id) / "memory" / "sessions" / session_id / "segments" / segment_id
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "summary.md").write_text(
        summary
        or "<t2_summary><segment_state>complete</segment_state><content>canonical summary</content></t2_summary>",
        encoding="utf-8",
    )
    (package_dir / "labels.md").write_text(
        labels
        or "<t2_labels><continuity_state>standalone</continuity_state><event_label>feedback</event_label></t2_labels>",
        encoding="utf-8",
    )
    (package_dir / "review.md").write_text(review, encoding="utf-8")
    (package_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.segment-package.manifest.v1",
                "package_status": "reviewed",
                "source_refs": source_refs or [f"t0://session/{session_id}/segment/{segment_id}#seq=1..2"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return package_dir


def test_reset_heartbeat_session_clears_only_maintenance_caches(agent_id: uuid.UUID) -> None:
    _heartbeat_tick_counts[agent_id] = 5
    _t2_mtimes[agent_id] = {"memory/sessions/s1/segments/seg-1": 1000.0}

    _reset_heartbeat_session(agent_id)

    assert agent_id not in _heartbeat_tick_counts
    assert agent_id not in _t2_mtimes


def test_read_t2_full_uses_canonical_segment_packages(agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
    _write_t2_package(
        tmp_agent_dir,
        agent_id,
        summary="<t2_summary><segment_state>complete</segment_state><content>User likes concise output</content></t2_summary>",
        review="<t2_review><decision>approved</decision><allowed_next>t3_intake</allowed_next><note>timeout</note></t2_review>",
    )

    with patch("app.config.get_settings") as mock:
        mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
        result = _read_t2_full(agent_id)

    assert "User likes concise" in result
    assert "timeout" in result
    assert "sessions/s1/segments/seg-1" in result
    assert "source_refs" in result


def test_read_incremental_t2_detects_changed_package(agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
    package_dir = _write_t2_package(tmp_agent_dir, agent_id)

    with patch("app.config.get_settings") as mock:
        mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
        _read_t2_full(agent_id)
        (package_dir / "summary.md").write_text(
            "<t2_summary><segment_state>complete</segment_state><content>changed</content></t2_summary>",
            encoding="utf-8",
        )
        result = _read_incremental_t2(agent_id)

    assert "changed" in result


def test_read_t3_summary_reads_two_plane_memory(agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
    memory_dir = tmp_agent_dir / str(agent_id) / "memory"
    (memory_dir / "self").mkdir(parents=True, exist_ok=True)
    (memory_dir / "self" / "self.md").write_text("## 能力\n\n### 深度研究 — 熟练\nsnake_case 偏好经验。\n")
    (memory_dir / "knowledge").mkdir(parents=True, exist_ok=True)
    (memory_dir / "knowledge" / "postgresql.md").write_text("---\ntitle: PostgreSQL\n---\n")

    with patch("app.config.get_settings") as mock:
        mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
        result = _read_t3_summary(agent_id)

    assert "snake_case" in result
    assert "postgresql" in result


def test_read_pending_t3_intake_uses_direct_core_language(agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
    _write_t2_package(tmp_agent_dir, agent_id, session_id="session-1", segment_id="seg-1")

    with patch("app.config.get_settings") as mock:
        mock.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
        result = _read_pending_t3_intake(agent_id)

    assert "T3 Consolidation Job Ready" in result
    assert "direct core reads `source_bundle.json`" in result
    assert "submit_t3_" not in result
