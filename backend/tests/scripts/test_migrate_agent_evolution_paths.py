from __future__ import annotations

import json
from pathlib import Path


def test_migrate_agent_evolution_paths_dry_run_does_not_write(tmp_path: Path) -> None:
    from app.scripts.migrate_agent_evolution_paths import migrate_workspace

    workspace = tmp_path / "agent-1"
    (workspace / "evolution" / "soul_candidates" / "cand-1").mkdir(parents=True)
    (workspace / "evolution" / "soul_candidates" / "cand-1" / "manifest.json").write_text("{}", encoding="utf-8")
    (workspace / "evolution" / "scorecard.md").write_text("# score\n", encoding="utf-8")

    report = migrate_workspace(workspace, apply=False)

    assert report["apply"] is False
    assert "copy_legacy_file:evolution/scorecard.md" in report["planned_operations"]
    assert "copy_soul_candidate:evolution/soul_candidates/cand-1" in report["planned_operations"]
    assert not (workspace / "memory" / ".legacy").exists()
    assert not (workspace / "memory" / ".staging").exists()


def test_migrate_agent_evolution_paths_apply_copies_to_unified_memory_paths(tmp_path: Path) -> None:
    from app.scripts.migrate_agent_evolution_paths import migrate_workspace

    workspace = tmp_path / "agent-1"
    (workspace / "evolution" / "soul_candidates" / "cand-1").mkdir(parents=True)
    (workspace / "evolution" / "soul_candidates" / "cand-1" / "manifest.json").write_text(
        '{"candidate_id":"cand-1"}',
        encoding="utf-8",
    )
    (workspace / "evolution" / "rollback" / "soul").mkdir(parents=True)
    (workspace / "evolution" / "rollback" / "soul" / "cand-1.soul.md.before").write_text(
        "# old soul\n",
        encoding="utf-8",
    )
    (workspace / "evolution" / "lineage.md").write_text("# lineage\n", encoding="utf-8")

    report = migrate_workspace(workspace, apply=True)

    assert report["apply"] is True
    assert (workspace / "memory" / ".staging" / "soul_candidates" / "cand-1" / "manifest.json").exists()
    assert (workspace / "memory" / ".rollback" / "soul" / "cand-1.soul.md.before").exists()
    assert (workspace / "memory" / ".legacy" / "evolution" / "lineage.md").exists()
    report_path = workspace / "memory" / ".legacy" / "evolution" / "migration_report.json"
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["legacy_sources_retained"] is True
    assert "copy_soul_rollback:evolution/rollback/soul/cand-1.soul.md.before" in persisted["applied_operations"]
