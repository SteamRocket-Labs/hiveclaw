from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


_MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "runtime_budget_breaker_dims_0709.py"
_RUN_METADATA_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "runtime_budget_run_metadata_0709.py"
)
_TEAM_SESSIONS_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "runtime_budget_team_sessions_0704.py"
)


class _ResumeSafeOp:
    def add_column(self, table_name: str, column: object, **kwargs: object) -> None:
        assert kwargs.get("if_not_exists") is True, f"{table_name}.{getattr(column, 'name', '?')} is not resume-safe"

    def create_index(self, index_name: str, table_name: str, columns: list[str], **kwargs: object) -> None:
        assert kwargs.get("if_not_exists") is True, f"{table_name}.{index_name} is not resume-safe"


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_budget_breaker_downgrade_only_drops_columns_created_by_upgrade() -> None:
    src = _MIGRATION.read_text(encoding="utf-8")
    upgrade_src = src.split("def upgrade() -> None:", 1)[1].split("def downgrade() -> None:", 1)[0]
    downgrade_src = src.split("def downgrade() -> None:", 1)[1]

    assert '"parent_invocations"' in upgrade_src
    assert 'op.drop_column("runtime_budget_runs", "parent_invocations")' in downgrade_src
    assert 'op.drop_column("runtime_budget_runs", "needs_reconciliation_count")' not in downgrade_src
    assert 'op.drop_column("runtime_budget_runs", "failures")' not in downgrade_src


def test_runtime_budget_breaker_upgrade_is_resume_safe() -> None:
    migration = _load_migration(_MIGRATION)

    migration.op = _ResumeSafeOp()
    migration.upgrade()


def test_runtime_budget_metadata_upgrade_is_resume_safe() -> None:
    migration = _load_migration(_RUN_METADATA_MIGRATION)

    migration.op = _ResumeSafeOp()
    migration.upgrade()


def test_runtime_budget_team_sessions_upgrade_is_resume_safe() -> None:
    migration = _load_migration(_TEAM_SESSIONS_MIGRATION)

    migration.op = _ResumeSafeOp()
    migration.upgrade()
