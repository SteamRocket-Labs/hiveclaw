from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from migration_snapshots.session_v2_permission_tool_contract_0716 import (
    DOWNGRADE_SQL,
    UPGRADE_SQL,
)


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def test_permission_tool_revision_extends_active_run_fence() -> None:
    source = (Path(__file__).parents[2] / "alembic" / "versions" / "session_v2_permission_tool_0716.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "session_v2_permission_tool_0716"' in source
    assert 'down_revision = "session_v2_round_outcome_0716"' in source
    assert "permission_state IN ('not_required','waiting','approved','denied','expired','cancelled')" in UPGRADE_SQL
    assert "status IN ('pending', 'running', 'suspended', 'resumable')" in UPGRADE_SQL
    assert "duplicate_active_session_run_during_permission_migration" in UPGRADE_SQL
    assert "status IN ('pending', 'running')" in DOWNGRADE_SQL


async def test_permission_tool_schema_keeps_waiting_and_resumable_runs_active(
    owner_sessionmaker,
) -> None:
    async with owner_sessionmaker() as db:
        permission_state_default = await db.scalar(
            text(
                """
                SELECT column_default
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name='session_tool_invocations'
                  AND column_name='permission_state'
                """
            )
        )
        index_definition = await db.scalar(
            text(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE schemaname='public'
                  AND tablename='runtime_tasks'
                  AND indexname='uq_runtime_tasks_active_web_chat_session'
                """
            )
        )
    assert permission_state_default is not None and "not_required" in permission_state_default
    assert index_definition is not None
    assert "suspended" in index_definition
    assert "resumable" in index_definition
