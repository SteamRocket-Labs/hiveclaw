from __future__ import annotations

import uuid
from pathlib import Path

from app.memory.lifecycle_store import MemoryLifecycleStore, lifecycle_path


def _seed_dirty_workspace(data_root: Path, agent_id: uuid.UUID) -> Path:
    workspace = data_root / str(agent_id)
    mem = workspace / "memory"
    (mem / "t3").mkdir(parents=True)
    (mem / "t3" / "user.md").write_text(
        "# Feedback\n\n"
        "- [2026-06-04][entry_id=f1][sensitivity=PL2_pii][status=active][version=1]"
        "[access_count=7][last_accessed=2026-06-04T17:00:00+00:00] vendor contact lives in CRM\n",
        encoding="utf-8",
    )
    (workspace / "memory.sqlite3").write_text("retired sqlite store", encoding="utf-8")
    (mem / "memory.json").write_text('{"legacy": true}', encoding="utf-8")
    (workspace / "reflections.md").write_text("# dead reflection stub\n", encoding="utf-8")
    (mem / "reflections.md").write_text("# old reflection stub\n", encoding="utf-8")
    reflections_dir = mem / "reflections"
    reflections_dir.mkdir()
    (reflections_dir / "kept.jsonl").write_text("{}\n", encoding="utf-8")
    return workspace


def test_agent_memory_hygiene_dry_run_reports_without_mutating(tmp_path: Path) -> None:
    from app.memory.hygiene import repair_agent_memory_hygiene

    agent_id = uuid.uuid4()
    workspace = _seed_dirty_workspace(tmp_path, agent_id)

    report = repair_agent_memory_hygiene(tmp_path, agent_id, dry_run=True)

    assert report["dry_run"] is True
    assert report["entries_migrated"] == 1
    assert {item["path"] for item in report["retired_artifacts"]} == {"memory.sqlite3", "memory/memory.json"}
    assert {item["path"] for item in report["dead_stubs"]} == {"reflections.md", "memory/reflections.md"}
    assert "[access_count=7]" in (workspace / "memory" / "t3" / "user.md").read_text(encoding="utf-8")
    assert (workspace / "memory.sqlite3").exists()
    assert (workspace / "memory" / "memory.json").exists()
    assert (workspace / "reflections.md").exists()
    assert (workspace / "memory" / "reflections" / "kept.jsonl").exists()
    assert not (workspace / "memory" / "retired_artifacts").exists()


def test_agent_memory_hygiene_apply_backfills_and_quarantines(tmp_path: Path) -> None:
    from app.memory.hygiene import repair_agent_memory_hygiene

    agent_id = uuid.uuid4()
    workspace = _seed_dirty_workspace(tmp_path, agent_id)

    report = repair_agent_memory_hygiene(tmp_path, agent_id, dry_run=False)

    assert report["dry_run"] is False
    assert report["entries_migrated"] == 1
    assert not (workspace / "memory.sqlite3").exists()
    assert not (workspace / "memory" / "memory.json").exists()
    assert not (workspace / "reflections.md").exists()
    assert not (workspace / "memory" / "reflections.md").exists()
    assert (workspace / "memory" / "reflections" / "kept.jsonl").exists()

    feedback = (workspace / "memory" / "t3" / "user.md").read_text(encoding="utf-8")
    assert feedback.strip().endswith("- [2026-06-04][entry_id=f1] vendor contact lives in CRM")
    assert "[sensitivity=" not in feedback
    assert "[access_count=" not in feedback

    lifecycle = MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id))
    entry = lifecycle.get("f1")
    assert entry.metadata["sensitivity"] == "PL2_pii"
    assert entry.access_count == 7
    assert entry.last_accessed is not None
    assert entry.last_accessed.isoformat().startswith("2026-06-04T17:00:00")

    archive = (workspace / "memory" / "archive.md").read_text(encoding="utf-8")
    assert "[access_count=7]" in archive
    assert "memory.sqlite3" in archive
    assert "memory/memory.json" in archive
    assert "reflections.md" in archive

    quarantined_targets = {item["target"] for item in report["retired_artifacts"] + report["dead_stubs"]}
    for target in quarantined_targets:
        assert (workspace / target).exists()


def test_all_workspace_memory_hygiene_scans_only_uuid_agents(tmp_path: Path) -> None:
    from app.memory.hygiene import repair_all_memory_hygiene

    agent_id = uuid.uuid4()
    _seed_dirty_workspace(tmp_path, agent_id)
    ignored = tmp_path / "enterprise_info_tenant"
    ignored.mkdir()
    (ignored / "memory.sqlite3").write_text("not an agent workspace", encoding="utf-8")

    report = repair_all_memory_hygiene(tmp_path, dry_run=False)

    assert report["schema"] == "memory_hygiene_report.v1"
    assert report["agents_scanned"] == 1
    assert report["agents_changed"] == 1
    assert report["entries_migrated"] == 1
    assert (ignored / "memory.sqlite3").exists()
