"""Migration smoke test for ``add_agent_plan_requests_0529``.

Mirrors the repo's existing migration tests (e.g.
``test_sso_scan_sessions_updated_at_migration.py``): load the migration module
in isolation, swap in a fake ``op``, and assert the SQL it would emit. This
keeps the migration test-paired without a live Postgres connection (no DB
engine fixtures exist in this suite).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    migration_path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "add_agent_plan_requests_0529.py"
    spec = importlib.util.spec_from_file_location("add_agent_plan_requests_0529", migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeOp:
    def __init__(self) -> None:
        self.created_tables: list[str] = []
        self.create_table_kwargs: list[tuple] = []
        self.statements: list[str] = []
        self.dropped_tables: list[str] = []

    def get_bind(self):
        class _Conn:
            def execute(self_inner, *_args, **_kwargs):
                class _R:
                    def scalar(self_innermost):
                        return None  # table does not exist -> create path

                return _R()

        return _Conn()

    def create_table(self, name, *columns):
        self.created_tables.append(name)
        self.create_table_kwargs.append(columns)

    def execute(self, statement) -> None:
        self.statements.append(str(statement))

    def drop_table(self, name) -> None:
        self.dropped_tables.append(name)


def test_revision_chain_points_at_current_head():
    module = _load_migration()
    assert module.revision == "add_agent_plan_requests_0529"
    assert module.down_revision == "raise_agent_tool_round_defaults_0526"


def test_upgrade_creates_table_and_all_indexes():
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.upgrade()

    assert fake_op.created_tables == ["agent_plan_requests"]

    joined = "\n".join(fake_op.statements)
    # The four documented composite indexes (§6.1).
    assert "(agent_id, status)" in joined
    assert "(tenant_id, status)" in joined
    assert "(session_id, created_at)" in joined
    assert "ix_agent_plan_requests_runtime_task " in joined
    assert "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_id UUID" in joined
    assert "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_version INTEGER" in joined
    assert "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_hash VARCHAR(80)" in joined
    assert "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_exempt_reason VARCHAR(100)" in joined
    # Index creates and task-column additions are both idempotent.
    assert all(
        s.startswith("CREATE INDEX IF NOT EXISTS") or s.startswith("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS")
        for s in fake_op.statements
    )


def test_downgrade_drops_table_and_indexes():
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.downgrade()

    assert fake_op.dropped_tables == ["agent_plan_requests"]
    assert all(
        s.startswith("DROP INDEX IF EXISTS") or s.startswith("ALTER TABLE tasks DROP COLUMN IF EXISTS")
        for s in fake_op.statements
    )
    # The four composite indexes are dropped too.
    assert any("ix_agent_plan_requests_agent_status" in s for s in fake_op.statements)
    assert any("ix_agent_plan_requests_tenant_status" in s for s in fake_op.statements)
    assert any("ALTER TABLE tasks DROP COLUMN IF EXISTS plan_id" in s for s in fake_op.statements)
