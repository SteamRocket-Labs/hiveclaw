from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "agent_session_permission_default_0713.py"
)


def test_agent_session_permission_default_migration_is_safe_and_reversible() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "agent_session_permission_default_0713"' in source
    assert 'down_revision = "hr_draft_recovery_0712"' in source
    assert '"default_session_permission_mode"' in source
    assert 'server_default="default"' in source
    assert "nullable=False" in source
    assert "drop_column" in source
