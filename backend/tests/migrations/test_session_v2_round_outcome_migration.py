from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from migration_snapshots.session_v2_round_outcome_contract_0716 import (
    DOWNGRADE_GUARD_SQL,
    UPGRADE_SQL,
)


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def test_round_outcome_revision_is_frozen_after_input_dispatch() -> None:
    source = (Path(__file__).parents[2] / "alembic" / "versions" / "session_v2_round_outcome_0716.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "session_v2_round_outcome_0716"' in source
    assert 'down_revision = "session_v2_input_dispatch_0716"' in source
    assert "uq_session_next_round_plan_generation" in UPGRADE_SQL
    assert "uq_session_next_round_plan_hash" in UPGRADE_SQL
    assert "uq_session_next_round_plan_current" in UPGRADE_SQL
    assert "state IN ('committed','dispatched','needs_reconciliation')" in UPGRADE_SQL
    assert "plan_generation <> 1" in DOWNGRADE_GUARD_SQL
    assert "HAVING count(*) > 1" in DOWNGRADE_GUARD_SQL


async def test_round_outcome_schema_preserves_plan_generations_and_one_current_plan(
    owner_sessionmaker,
) -> None:
    async with owner_sessionmaker() as db:
        column = await db.scalar(
            text(
                """
                SELECT is_nullable || ':' || column_default
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name='session_next_round_plans'
                  AND column_name='plan_generation'
                """
            )
        )
        constraints = set(
            (
                await db.execute(
                    text(
                        """
                        SELECT conname
                        FROM pg_constraint
                        WHERE conrelid='public.session_next_round_plans'::regclass
                        """
                    )
                )
            ).scalars()
        )
        index_definition = await db.scalar(
            text(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE schemaname='public'
                  AND tablename='session_next_round_plans'
                  AND indexname='uq_session_next_round_plan_current'
                """
            )
        )
    assert column is not None and column.startswith("NO:") and "1" in column
    assert "uq_session_next_round_plan_generation" in constraints
    assert "uq_session_next_round_plan_hash" in constraints
    assert index_definition is not None
    assert "UNIQUE INDEX" in index_definition
    assert "committed" in index_definition and "dispatched" in index_definition
