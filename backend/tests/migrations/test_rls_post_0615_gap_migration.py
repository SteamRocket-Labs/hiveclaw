from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "rls_post_0615_gap_closure_0703.py"
    )
    spec = importlib.util.spec_from_file_location("rls_post_0615_gap_closure_0703", migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement) -> None:
        self.statements.append(str(statement))


def test_revision_extends_current_head() -> None:
    module = _load_migration()

    assert module.revision == "rls_post_0615_gap_closure_0703"
    assert module.down_revision == "token_quota_hard_caps_0703"


def test_upgrade_forces_rls_on_post_0615_tenant_and_child_tables() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op
    module._existing_tables = lambda: set(module._TENANT_TABLES) | set(module._TEAM_CHILD_TABLES)

    module.upgrade()

    joined = "\n".join(fake_op.statements)
    for table in (*module._TENANT_TABLES, *module._TEAM_CHILD_TABLES):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in joined
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in joined
        assert f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}" in joined
        assert f"CREATE POLICY tenant_isolation_{table} ON {table}" in joined
        assert "WITH CHECK" in joined

    assert "agent_teams.id = agent_team_members.team_id" in joined
    assert "agent_teams.id = agent_team_events.team_id" in joined
    assert "current_setting('app.current_tenant_id', true) = 'BYPASS'" in joined


def test_downgrade_removes_only_this_migrations_policies_and_force() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op
    module._existing_tables = lambda: set(module._TENANT_TABLES) | set(module._TEAM_CHILD_TABLES)

    module.downgrade()

    joined = "\n".join(fake_op.statements)
    for table in reversed((*module._TENANT_TABLES, *module._TEAM_CHILD_TABLES)):
        assert f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}" in joined
        assert f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY" in joined
        assert f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY" in joined
