from __future__ import annotations

from pathlib import Path


_MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "runtime_budget_breaker_dims_0709.py"


def test_runtime_budget_breaker_downgrade_only_drops_columns_created_by_upgrade() -> None:
    src = _MIGRATION.read_text(encoding="utf-8")
    upgrade_src = src.split("def upgrade() -> None:", 1)[1].split("def downgrade() -> None:", 1)[0]
    downgrade_src = src.split("def downgrade() -> None:", 1)[1]

    assert '"parent_invocations"' in upgrade_src
    assert 'op.drop_column("runtime_budget_runs", "parent_invocations")' in downgrade_src
    assert 'op.drop_column("runtime_budget_runs", "needs_reconciliation_count")' not in downgrade_src
    assert 'op.drop_column("runtime_budget_runs", "failures")' not in downgrade_src
