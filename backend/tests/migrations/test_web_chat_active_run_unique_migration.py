from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    migration_path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "web_chat_active_run_unique_0612.py"
    spec = importlib.util.spec_from_file_location("web_chat_active_run_unique_0612", migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def test_revision_chain_points_at_current_head() -> None:
    module = _load_migration()
    assert module.revision == "web_chat_active_run_unique_0612"
    assert module.down_revision == "token_usage_events_0612"


def test_upgrade_adds_active_web_chat_partial_unique_index() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.upgrade()

    assert len(fake_op.statements) == 2
    cleanup_sql = fake_op.statements[0]
    assert cleanup_sql.startswith("UPDATE runtime_tasks AS rt")
    assert "row_number() OVER" in cleanup_sql
    assert "PARTITION BY parent_agent_id, parent_session_id" in cleanup_sql
    assert "status IN ('pending', 'running')" in cleanup_sql
    assert "superseded_by_active_run_guard" in cleanup_sql
    assert fake_op.statements[1] == (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_tasks_active_web_chat_session "
        "ON runtime_tasks (parent_agent_id, parent_session_id) "
        "WHERE task_type = 'web_chat_turn' "
        "AND status IN ('pending', 'running') "
        "AND parent_agent_id IS NOT NULL "
        "AND parent_session_id IS NOT NULL"
    )


def test_downgrade_drops_active_web_chat_partial_unique_index() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.downgrade()

    assert fake_op.statements == ["DROP INDEX IF EXISTS uq_runtime_tasks_active_web_chat_session"]
