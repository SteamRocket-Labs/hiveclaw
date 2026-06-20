from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "agent_role_description_text_0620.py"
    )
    spec = importlib.util.spec_from_file_location("agent_role_description_text_0620", migration_path)
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
    assert module.revision == "agent_role_description_text_0620"
    assert module.down_revision == "decision_trace_pg_store_0615"


def test_upgrade_changes_agent_role_description_to_text() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.upgrade()

    assert fake_op.statements == ["ALTER TABLE agents ALTER COLUMN role_description TYPE TEXT"]


def test_downgrade_restores_agent_role_description_legacy_limit() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.downgrade()

    assert fake_op.statements == ["ALTER TABLE agents ALTER COLUMN role_description TYPE VARCHAR(500)"]
