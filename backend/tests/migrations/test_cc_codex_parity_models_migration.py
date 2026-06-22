from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    migration_path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "cc_codex_parity_goal_team_0622.py"
    spec = importlib.util.spec_from_file_location("cc_codex_parity_goal_team_0622", migration_path)
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

    def create_table(self, name, *columns, **kwargs):
        self.created_tables.append(name)

    def execute(self, statement) -> None:
        self.statements.append(str(statement))

    def drop_table(self, name) -> None:
        self.dropped_tables.append(name)


def test_revision_chain_points_at_current_head():
    module = _load_migration()
    assert module.revision == "cc_codex_parity_goal_team_0622"
    assert module.down_revision == "local_agent_bridge_0622"


def test_upgrade_creates_goal_and_team_control_indexes():
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.upgrade()

    assert fake_op.created_tables == [
        "agent_session_goals",
        "agent_teams",
        "agent_team_members",
        "agent_team_events",
    ]
    joined = "\n".join(fake_op.statements)
    assert "ix_agent_session_goals_active_session" in joined
    assert "ix_agent_teams_lead_session" in joined
    assert "ix_agent_team_members_team_status" in joined
    assert "ix_agent_team_events_team_created" in joined


def test_downgrade_drops_goal_and_team_tables_in_dependency_order():
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.downgrade()

    assert fake_op.dropped_tables == [
        "agent_team_events",
        "agent_team_members",
        "agent_teams",
        "agent_session_goals",
    ]
