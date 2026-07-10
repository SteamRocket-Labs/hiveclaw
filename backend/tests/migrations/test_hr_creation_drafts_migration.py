from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "hr_creation_drafts_0710.py"
    spec = importlib.util.spec_from_file_location("hr_creation_drafts_0710", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeOp:
    def __init__(self):
        self.tables = []
        self.indexes = []
        self.columns = []
        self.constraints = []
        self.statements = []

    def create_table(self, name, *_columns, **_kwargs):
        self.tables.append(name)

    def create_index(self, name, table, columns, **_kwargs):
        self.indexes.append((name, table, tuple(columns)))

    def add_column(self, table, column):
        self.columns.append((table, column.name))

    def create_foreign_key(self, name, *_args, **_kwargs):
        self.constraints.append(name)

    def create_unique_constraint(self, name, *_args, **_kwargs):
        self.constraints.append(name)

    def execute(self, statement):
        self.statements.append(str(statement))

    def drop_constraint(self, *_args, **_kwargs):
        return None

    def drop_column(self, *_args, **_kwargs):
        return None

    def drop_index(self, *_args, **_kwargs):
        return None

    def drop_table(self, *_args, **_kwargs):
        return None


def test_migration_creates_single_source_tenant_scoped_draft_ledger():
    module = _load_migration()
    fake = _FakeOp()
    module.op = fake

    module.upgrade()

    assert module.down_revision == "typed_thread_items_0710"
    assert fake.tables == ["hr_creation_drafts"]
    assert fake.columns == []
    assert fake.constraints == []
    assert ("ix_hr_creation_drafts_created_agent_id", "hr_creation_drafts", ("created_agent_id",)) in fake.indexes
    sql = "\n".join(fake.statements)
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "tenant_isolation_hr_creation_drafts" in sql


def test_fresh_bootstrap_applies_strict_forced_rls_to_hr_creation_drafts():
    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES, STRICT_TENANT_RLS_TABLES, _policy_predicates_for_table

    assert "hr_creation_drafts" in RLS_FORCED_TENANT_TABLES
    assert "hr_creation_drafts" in STRICT_TENANT_RLS_TABLES
    using, check = _policy_predicates_for_table("hr_creation_drafts")
    assert using == check
    assert "tenant_id IS NULL" not in using
