"""Safety contracts for exec/automation retirement migrations.

These tests intentionally inspect migration/script source. The retired objective
and supervision systems are production data surfaces, so the migration files
must prove they archive legacy rows before deleting or dropping incompatible
schema.
"""

from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (BACKEND_ROOT / path).read_text(encoding="utf-8")


def test_supervision_retirement_migration_archives_and_deletes_legacy_rows_before_drop() -> None:
    source = _read("alembic/versions/retire_task_supervision_0608.py")

    assert "retired_supervision_tasks_0608" in source
    assert "to_jsonb(tasks.*)" in source
    assert "DELETE FROM tasks WHERE type::text = 'supervision'" in source
    assert source.index("DELETE FROM tasks WHERE type::text = 'supervision'") < source.index(
        "ALTER TABLE tasks DROP COLUMN IF EXISTS"
    )


def test_objective_retirement_migration_archives_rows_before_dropping_table() -> None:
    source = _read("alembic/versions/retire_agent_objectives_0608.py")

    assert "retired_agent_objectives_0608" in source
    assert "to_jsonb(agent_objectives.*)" in source
    assert source.index("to_jsonb(agent_objectives.*)") < source.index("DROP TABLE IF EXISTS agent_objectives CASCADE")


def test_legacy_data_dryrun_reports_objective_rows_not_only_table_presence() -> None:
    source = _read("exec_align_legacy_data_dryrun.py")

    assert "_OBJECTIVE_ROWS_SQL" in source
    assert "agent_objectives_rows" in source
    assert "agent_objectives_active_rows" in source
    assert "objective_task triggers" in source
