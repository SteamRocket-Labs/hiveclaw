from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4


def _write_segment_package(root: Path, agent_id: object, *, base: str = "memory/t2/sessions") -> Path:
    package_dir = root / str(agent_id) / base / "s1" / "segments" / "seg-1"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "summary.md").write_text("<t2_summary />\n", encoding="utf-8")
    (package_dir / "labels.md").write_text("<t2_labels />\n", encoding="utf-8")
    (package_dir / "review.md").write_text("<t2_review />\n", encoding="utf-8")
    (package_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.segment-package.manifest.v1",
                "package_id": "pkg-seg",
                "package_status": "reviewed",
                "source_refs": ["t0://session/s1/segment/seg-1#seq=1..3"],
            }
        ),
        encoding="utf-8",
    )
    return package_dir


def _write_episode_package(root: Path, agent_id: object) -> Path:
    package_dir = root / str(agent_id) / "memory" / "t2" / "sessions" / "s1" / "episodes" / "episode-1"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "synthesis.md").write_text("<episode_synthesis />\n", encoding="utf-8")
    (package_dir / "review.md").write_text("<episode_review />\n", encoding="utf-8")
    (package_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.episode-stitch.manifest.v1",
                "episode_id": "episode-1",
                "package_status": "closed",
                "source_packages": ["pkg-seg"],
                "source_refs": ["t0://session/s1/segment/seg-1#seq=1..3"],
            }
        ),
        encoding="utf-8",
    )
    return package_dir


def test_t2_snapshot_keeps_segment_and_episode_with_same_source_ref(tmp_path: Path) -> None:
    from app.memory.t2.read_model import load_t2_package_snapshots

    agent_id = uuid4()
    _write_segment_package(tmp_path, agent_id)
    _write_episode_package(tmp_path, agent_id)

    snapshots, _mtimes = load_t2_package_snapshots(tmp_path, agent_id)

    assert {snapshot.rel_path for snapshot in snapshots} == {
        "memory/t2/sessions/s1/segments/seg-1",
        "memory/t2/sessions/s1/episodes/episode-1",
    }
    assert {snapshot.package_kind for snapshot in snapshots} == {"segment_package", "episode_stitch_package"}


def test_t2_snapshot_dedupes_legacy_duplicate_of_canonical_package(tmp_path: Path) -> None:
    from app.memory.t2.read_model import load_t2_package_snapshots

    agent_id = uuid4()
    _write_segment_package(tmp_path, agent_id)
    _write_segment_package(tmp_path, agent_id, base="memory/sessions")

    snapshots, _mtimes = load_t2_package_snapshots(tmp_path, agent_id)

    assert [snapshot.rel_path for snapshot in snapshots] == ["memory/t2/sessions/s1/segments/seg-1"]
