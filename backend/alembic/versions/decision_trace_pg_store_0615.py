"""Add tenant-scoped decision trace SQL store.

Revision ID: decision_trace_pg_store_0615
Revises: decision_trace_linkback_0615
Create Date: 2026-06-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "decision_trace_pg_store_0615"
down_revision = "decision_trace_linkback_0615"
branch_labels = None
depends_on = None


def _json_type():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def _json_default(value: str):
    if op.get_bind().dialect.name == "postgresql":
        return sa.text(f"'{value}'::jsonb")
    return sa.text(f"'{value}'")


def upgrade() -> None:
    json_type = _json_type()
    existing = set(inspect(op.get_bind()).get_table_names())
    if "decision_traces" not in existing:
        op.create_table(
            "decision_traces",
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("decision_id", sa.String(length=128), nullable=False),
            sa.Column("tenant_id", sa.UUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("agent_id", sa.UUID(), sa.ForeignKey("agents.id"), nullable=True),
            sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("session_id", sa.String(length=255), nullable=True),
            sa.Column("message_id", sa.String(length=255), nullable=True),
            sa.Column("tool_name", sa.String(length=200), nullable=True),
            sa.Column("checkpoint_id", sa.String(length=128), nullable=True),
            sa.Column("action", sa.String(length=255), nullable=False),
            sa.Column("chosen", sa.String(length=80), nullable=False),
            sa.Column("reasoning", sa.Text(), nullable=False),
            sa.Column("alternatives_json", json_type, nullable=False, server_default=_json_default("[]")),
            sa.Column("situational_factors_json", json_type, nullable=False, server_default=_json_default("[]")),
            sa.Column("charter_zone", sa.String(length=80), nullable=False),
            sa.Column("preflight_json", json_type, nullable=False, server_default=_json_default("{}")),
            sa.Column("sensitivity", sa.String(length=80), nullable=False),
            sa.Column("payload_json", json_type, nullable=False, server_default=_json_default("{}")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_decision_traces_decision_id", "decision_traces", ["decision_id"], unique=True)
        op.create_index("ix_decision_traces_tenant_id", "decision_traces", ["tenant_id"])
        op.create_index("ix_decision_traces_agent_id", "decision_traces", ["agent_id"])
        op.create_index("ix_decision_traces_user_id", "decision_traces", ["user_id"])
        op.create_index("ix_decision_traces_session_id", "decision_traces", ["session_id"])
        op.create_index("ix_decision_traces_message_id", "decision_traces", ["message_id"])
        op.create_index("ix_decision_traces_tool_name", "decision_traces", ["tool_name"])
        op.create_index("ix_decision_traces_checkpoint_id", "decision_traces", ["checkpoint_id"])
        op.create_index("ix_decision_traces_created_at", "decision_traces", ["created_at"])
        op.create_index("ix_decision_traces_tenant_session", "decision_traces", ["tenant_id", "session_id"])
        op.create_index("ix_decision_traces_tenant_agent", "decision_traces", ["tenant_id", "agent_id"])
        op.create_index("ix_decision_traces_tool_created", "decision_traces", ["tool_name", "created_at"])

    if "decision_trace_feedback" not in existing:
        op.create_table(
            "decision_trace_feedback",
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("decision_id", sa.String(length=128), nullable=False),
            sa.Column("tenant_id", sa.UUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("refs", sa.String(length=160), nullable=False),
            sa.Column("reaction", sa.String(length=80), nullable=False),
            sa.Column("polarity", sa.String(length=40), nullable=False),
            sa.Column("source", sa.String(length=80), nullable=False),
            sa.Column("rationale_from_owner", sa.Text(), nullable=False, server_default=""),
            sa.Column("payload_json", json_type, nullable=False, server_default=_json_default("{}")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_decision_trace_feedback_decision_id", "decision_trace_feedback", ["decision_id"])
        op.create_index("ix_decision_trace_feedback_tenant_id", "decision_trace_feedback", ["tenant_id"])
        op.create_index("ix_decision_trace_feedback_created", "decision_trace_feedback", ["created_at"])
        op.create_index(
            "ix_decision_trace_feedback_tenant_decision",
            "decision_trace_feedback",
            ["tenant_id", "decision_id"],
        )

    if op.get_bind().dialect.name == "postgresql":
        for table in ("decision_traces", "decision_trace_feedback"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"""
                DO $$ BEGIN
                    CREATE POLICY tenant_isolation_{table} ON {table}
                        USING (
                            current_setting('app.current_tenant_id', true) = 'BYPASS'
                            OR tenant_id::text = current_setting('app.current_tenant_id', true)
                            OR tenant_id IS NULL
                        );
                EXCEPTION WHEN duplicate_object THEN NULL;
                END $$
                """
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation_decision_trace_feedback ON decision_trace_feedback")
        op.execute("DROP POLICY IF EXISTS tenant_isolation_decision_traces ON decision_traces")
    op.drop_table("decision_trace_feedback")
    op.drop_table("decision_traces")
