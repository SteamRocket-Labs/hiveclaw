from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    migration_path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "add_agent_work_ledgers_0601.py"
    spec = importlib.util.spec_from_file_location("add_agent_work_ledgers_0601", migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeOp:
    def __init__(self) -> None:
        self.created_tables: list[str] = []
        self.statements: list[str] = []
        self.dropped_tables: list[str] = []

    def get_bind(self):
        class _Conn:
            def execute(self_inner, *_args, **_kwargs):
                class _R:
                    def scalar(self_innermost):
                        return None

                return _R()

        return _Conn()

    def create_table(self, name, *columns):
        self.created_tables.append(name)

    def execute(self, statement) -> None:
        self.statements.append(str(statement))

    def drop_table(self, name) -> None:
        self.dropped_tables.append(name)


def test_revision_chain_points_at_plan_recommendation_head():
    module = _load_migration()
    assert module.revision == "add_agent_work_ledgers_0601"
    assert module.down_revision == "add_agent_plan_recommendations_0531"


def test_upgrade_creates_agent_work_ledgers_table_and_indexes():
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.upgrade()

    assert fake_op.created_tables == ["agent_work_ledgers"]
    joined = "\n".join(fake_op.statements)
    assert "ix_agent_work_ledgers_agent_status" in joined
    assert "ix_agent_work_ledgers_plan_id" in joined
    assert "ix_agent_work_ledgers_runtime_task_id" in joined
    assert "ix_agent_work_ledgers_tenant_status" in joined
    assert all(statement.startswith("CREATE INDEX IF NOT EXISTS") for statement in fake_op.statements)


def test_downgrade_drops_agent_work_ledgers_table_and_indexes():
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.downgrade()

    assert fake_op.dropped_tables == ["agent_work_ledgers"]
    assert all(statement.startswith("DROP INDEX IF EXISTS") for statement in fake_op.statements)
