from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "agent_identity_lifecycle_0613.py"
    )
    spec = importlib.util.spec_from_file_location("agent_identity_lifecycle_0613", migration_path)
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
    assert module.revision == "agent_identity_lifecycle_0613"
    assert module.down_revision == "chat_message_thinking_signature_0613"


def test_upgrade_adds_identity_and_lifecycle_columns_with_backfill() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.upgrade()

    sql = "\n".join(fake_op.statements)
    assert "ADD COLUMN IF NOT EXISTS sponsor_user_id UUID" in sql
    assert "ADD COLUMN IF NOT EXISTS participant_id UUID" in sql
    assert "ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE" in sql
    assert "ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMP WITH TIME ZONE" in sql
    assert "ADD COLUMN IF NOT EXISTS deactivation_reason TEXT" in sql
    assert "INSERT INTO participants" in sql
    assert "ON CONFLICT (type, ref_id) DO UPDATE" in sql
    assert "UPDATE agents AS a" in sql
    assert "COALESCE(a.owner_user_id, a.creator_id)" in sql
    assert "ALTER COLUMN sponsor_user_id SET NOT NULL" in sql
    assert "ALTER COLUMN participant_id SET NOT NULL" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_agents_sponsor_user_id" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_agents_participant_id" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_agents_active_lifecycle" in sql


def test_downgrade_removes_identity_and_lifecycle_columns() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.downgrade()

    sql = "\n".join(fake_op.statements)
    assert "DROP COLUMN IF EXISTS sponsor_user_id" in sql
    assert "DROP COLUMN IF EXISTS participant_id" in sql
    assert "DROP COLUMN IF EXISTS deleted_at" in sql
    assert "DROP COLUMN IF EXISTS deactivated_at" in sql
    assert "DROP COLUMN IF EXISTS deactivation_reason" in sql
