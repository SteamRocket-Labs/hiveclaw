from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    migration_path = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "add_unique_system_hr_agent_per_tenant_0501.py"
    )
    spec = importlib.util.spec_from_file_location("add_unique_system_hr_agent_per_tenant_0501", migration_path)
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


def test_upgrade_adds_partial_unique_index_for_system_hr_agents() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.upgrade()

    assert fake_op.statements == [
        (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_one_system_hr_per_tenant "
            "ON agents (tenant_id) "
            "WHERE tenant_id IS NOT NULL "
            "AND agent_class = 'internal_system' "
            "AND name = '__system_hr__'"
        )
    ]


def test_downgrade_drops_partial_unique_index_for_system_hr_agents() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.downgrade()

    assert fake_op.statements == ["DROP INDEX IF EXISTS uq_agents_one_system_hr_per_tenant"]
