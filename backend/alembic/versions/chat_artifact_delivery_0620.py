"""Add chat artifact delivery transcript tables.

Revision ID: chat_artifact_delivery_0620
Revises: agent_role_description_text_0620
Create Date: 2026-06-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "chat_artifact_delivery_0620"
down_revision = "agent_role_description_text_0620"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table: str) -> bool:
    return _inspector().has_table(table)


def _column_exists(table: str, column: str) -> bool:
    return any(existing["name"] == column for existing in _inspector().get_columns(table))


def _fk_exists_for_columns(table: str, columns: list[str]) -> bool:
    target = tuple(columns)
    return any(tuple(fk.get("constrained_columns") or []) == target for fk in _inspector().get_foreign_keys(table))


def _add_chat_session_column_if_missing(column: sa.Column) -> None:
    if not _column_exists("chat_sessions", column.name):
        op.add_column("chat_sessions", column)


def upgrade() -> None:
    _add_chat_session_column_if_missing(sa.Column("session_kind", sa.String(length=64), nullable=False, server_default="human_chat"))
    _add_chat_session_column_if_missing(sa.Column("actor_type", sa.String(length=32), nullable=False, server_default="user"))
    _add_chat_session_column_if_missing(sa.Column("runtime_source", sa.String(length=64), nullable=False, server_default="web_chat"))
    _add_chat_session_column_if_missing(sa.Column("visibility_scope", sa.String(length=64), nullable=False, server_default="direct_user"))
    _add_chat_session_column_if_missing(sa.Column("listed_surface", sa.String(length=64), nullable=False, server_default="chat"))
    _add_chat_session_column_if_missing(sa.Column("parent_session_id", postgresql.UUID(as_uuid=True), nullable=True))
    _add_chat_session_column_if_missing(sa.Column("root_session_id", postgresql.UUID(as_uuid=True), nullable=True))
    _add_chat_session_column_if_missing(sa.Column("runtime_task_id", postgresql.UUID(as_uuid=True), nullable=True))
    _add_chat_session_column_if_missing(sa.Column("transcript_metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_sessions_listed_surface ON chat_sessions (listed_surface)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_sessions_runtime_task_id ON chat_sessions (runtime_task_id)")
    if not _fk_exists_for_columns("chat_sessions", ["parent_session_id"]):
        op.create_foreign_key("fk_chat_sessions_parent_session_id", "chat_sessions", "chat_sessions", ["parent_session_id"], ["id"])
    if not _fk_exists_for_columns("chat_sessions", ["root_session_id"]):
        op.create_foreign_key("fk_chat_sessions_root_session_id", "chat_sessions", "chat_sessions", ["root_session_id"], ["id"])
    if not _fk_exists_for_columns("chat_sessions", ["runtime_task_id"]):
        op.create_foreign_key("fk_chat_sessions_runtime_task_id", "chat_sessions", "runtime_tasks", ["runtime_task_id"], ["id"])

    if not _table_exists("chat_transcript_events"):
        op.create_table(
            "chat_transcript_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("sequence", sa.BigInteger(), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("parent_event_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("root_session_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("parent_session_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("actor_type", sa.String(length=32), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("visibility_scope", sa.String(length=64), nullable=False, server_default="direct_user"),
            sa.Column("listed_surface", sa.String(length=64), nullable=False, server_default="chat"),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("parts_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"]),
            sa.ForeignKeyConstraint(["run_id"], ["runtime_tasks.id"]),
            sa.ForeignKeyConstraint(["parent_event_id"], ["chat_transcript_events.id"]),
            sa.ForeignKeyConstraint(["root_session_id"], ["chat_sessions.id"]),
            sa.ForeignKeyConstraint(["parent_session_id"], ["chat_sessions.id"]),
            sa.ForeignKeyConstraint(["message_id"], ["chat_messages.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("session_id", "sequence", name="uq_chat_transcript_events_session_sequence"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_transcript_events_agent_id ON chat_transcript_events (agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_transcript_events_tenant_id ON chat_transcript_events (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_transcript_events_session_sequence ON chat_transcript_events (session_id, sequence)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_transcript_events_run_id ON chat_transcript_events (run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_transcript_events_message_id ON chat_transcript_events (message_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_transcript_events_listed_surface ON chat_transcript_events (listed_surface)")
    op.execute("ALTER TABLE chat_transcript_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chat_transcript_events FORCE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation_chat_transcript_events ON chat_transcript_events
                USING (
                    current_setting('app.current_tenant_id', true) = 'BYPASS'
                    OR tenant_id::text = current_setting('app.current_tenant_id', true)
                    OR tenant_id IS NULL
                );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    if not _table_exists("chat_artifacts"):
        op.create_table(
            "chat_artifacts",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("runtime_task_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("path", sa.String(length=1024), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("mime_type", sa.String(length=255), nullable=True),
            sa.Column("size", sa.Integer(), nullable=True),
            sa.Column("modified_at", sa.String(length=64), nullable=True),
            sa.Column("preview_kind", sa.String(length=32), nullable=False, server_default="download"),
            sa.Column("source", sa.String(length=64), nullable=False, server_default="workspace_write"),
            sa.Column("snapshot_hash", sa.String(length=128), nullable=False),
            sa.Column("snapshot_json", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"]),
            sa.ForeignKeyConstraint(["message_id"], ["chat_messages.id"]),
            sa.ForeignKeyConstraint(["runtime_task_id"], ["runtime_tasks.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "agent_id",
                "session_id",
                "runtime_task_id",
                "path",
                "snapshot_hash",
                name="uq_chat_artifacts_agent_session_run_path_snapshot",
            ),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_artifacts_agent_id ON chat_artifacts (agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_artifacts_tenant_id ON chat_artifacts (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_artifacts_runtime_task_id ON chat_artifacts (runtime_task_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_artifacts_message_id ON chat_artifacts (message_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_artifacts_session_id ON chat_artifacts (session_id)")
    op.execute("ALTER TABLE chat_artifacts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chat_artifacts FORCE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation_chat_artifacts ON chat_artifacts
                USING (
                    current_setting('app.current_tenant_id', true) = 'BYPASS'
                    OR tenant_id::text = current_setting('app.current_tenant_id', true)
                    OR tenant_id IS NULL
                );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_chat_transcript_events ON chat_transcript_events")
    op.execute("ALTER TABLE chat_transcript_events DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_chat_artifacts ON chat_artifacts")
    op.execute("ALTER TABLE chat_artifacts DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_chat_artifacts_session_id", table_name="chat_artifacts")
    op.drop_index("ix_chat_artifacts_message_id", table_name="chat_artifacts")
    op.drop_index("ix_chat_artifacts_runtime_task_id", table_name="chat_artifacts")
    op.drop_index("ix_chat_artifacts_tenant_id", table_name="chat_artifacts")
    op.drop_index("ix_chat_artifacts_agent_id", table_name="chat_artifacts")
    op.drop_table("chat_artifacts")
    op.drop_index("ix_chat_transcript_events_listed_surface", table_name="chat_transcript_events")
    op.drop_index("ix_chat_transcript_events_message_id", table_name="chat_transcript_events")
    op.drop_index("ix_chat_transcript_events_run_id", table_name="chat_transcript_events")
    op.drop_index("ix_chat_transcript_events_session_sequence", table_name="chat_transcript_events")
    op.drop_index("ix_chat_transcript_events_tenant_id", table_name="chat_transcript_events")
    op.drop_index("ix_chat_transcript_events_agent_id", table_name="chat_transcript_events")
    op.drop_table("chat_transcript_events")
    op.drop_constraint("fk_chat_sessions_runtime_task_id", "chat_sessions", type_="foreignkey")
    op.drop_constraint("fk_chat_sessions_root_session_id", "chat_sessions", type_="foreignkey")
    op.drop_constraint("fk_chat_sessions_parent_session_id", "chat_sessions", type_="foreignkey")
    op.drop_index("ix_chat_sessions_runtime_task_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_listed_surface", table_name="chat_sessions")
    op.drop_column("chat_sessions", "transcript_metadata_json")
    op.drop_column("chat_sessions", "runtime_task_id")
    op.drop_column("chat_sessions", "root_session_id")
    op.drop_column("chat_sessions", "parent_session_id")
    op.drop_column("chat_sessions", "listed_surface")
    op.drop_column("chat_sessions", "visibility_scope")
    op.drop_column("chat_sessions", "runtime_source")
    op.drop_column("chat_sessions", "actor_type")
    op.drop_column("chat_sessions", "session_kind")
