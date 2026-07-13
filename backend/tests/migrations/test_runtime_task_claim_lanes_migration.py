from __future__ import annotations

from pathlib import Path


def test_runtime_task_claim_lanes_migration_is_single_head_successor_with_partial_indexes():
    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "runtime_task_claim_lanes_0713.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision = "system_plan_outbox_0713"' in migration
    assert "ix_runtime_tasks_claim_normal_lane" in migration
    assert "ix_runtime_tasks_claim_aged_lane" in migration
    assert "ix_runtime_tasks_claim_expired_lane" in migration
    assert "postgresql_where" in migration
