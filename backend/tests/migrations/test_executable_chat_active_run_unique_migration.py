from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "executable_chat_active_run_unique_0622.py"
    )
    spec = importlib.util.spec_from_file_location("executable_chat_active_run_unique_0622", migration_path)
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


def test_revision_chain_extends_cc_codex_parity_head() -> None:
    module = _load_migration()
    assert module.revision == "executable_chat_active_run_unique_0622"
    assert module.down_revision == "cc_codex_parity_goal_team_0622"


def test_upgrade_replaces_web_chat_only_index_with_all_executable_chat_task_types() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.upgrade()

    joined = "\n".join(fake_op.statements)
    assert "DROP INDEX IF EXISTS uq_runtime_tasks_active_web_chat_session" in joined
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_tasks_active_web_chat_session" in joined
    assert "task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan')" in joined
    assert "superseded_by_executable_chat_active_run_guard" in joined


def test_downgrade_restores_web_chat_only_guard() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.downgrade()

    joined = "\n".join(fake_op.statements)
    assert "DROP INDEX IF EXISTS uq_runtime_tasks_active_web_chat_session" in joined
    assert "WHERE task_type = 'web_chat_turn'" in joined
