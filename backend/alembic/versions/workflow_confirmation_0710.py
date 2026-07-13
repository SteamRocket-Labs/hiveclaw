"""Add durable Dynamic Workflow proposal and preview confirmation artifacts.

Revision ID: workflow_confirmation_0710
Revises: agent_authority_0710
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.migration_compat import create_index_if_missing, create_table_if_missing


revision = "workflow_confirmation_0710"
down_revision = "agent_authority_0710"
branch_labels = None
depends_on = None

_TABLES = ("workflow_proposal_artifacts", "workflow_preview_artifacts")


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table} ON {table}
        USING (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        WITH CHECK (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        """
    )


def upgrade() -> None:
    create_table_if_missing(
        op,
        "workflow_proposal_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), server_default="open", nullable=False),
        sa.Column("artifact_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("artifact_hash", sa.String(length=80), nullable=False),
        sa.Column("proposal_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('open','previewed','expired')", name="ck_workflow_proposal_artifact_status"),
    )
    for name, columns in (
        ("ix_workflow_proposal_artifacts_tenant_id", ["tenant_id"]),
        ("ix_workflow_proposal_artifacts_agent_id", ["agent_id"]),
        ("ix_workflow_proposal_artifacts_session_id", ["session_id"]),
        ("ix_workflow_proposal_artifacts_requested_by_user_id", ["requested_by_user_id"]),
        ("ix_workflow_proposal_artifacts_expires_at", ["expires_at"]),
        ("ix_workflow_proposal_identity", ["tenant_id", "agent_id", "session_id", "requested_by_user_id"]),
    ):
        create_index_if_missing(op, name, "workflow_proposal_artifacts", columns)

    create_table_if_missing(
        op,
        "workflow_preview_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "proposal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflow_proposal_artifacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("candidate_id", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="ready", nullable=False),
        sa.Column("artifact_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("artifact_hash", sa.String(length=80), nullable=False),
        sa.Column("definition_hash", sa.String(length=80), nullable=False),
        sa.Column("args_hash", sa.String(length=80), nullable=False),
        sa.Column("definition_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("args_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("preview_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "confirmed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("confirmation_source", sa.String(length=64), nullable=True),
        sa.Column("confirmation_evidence_id", sa.String(length=200), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ready','starting','started','failed','expired')",
            name="ck_workflow_preview_artifact_status",
        ),
        sa.UniqueConstraint("run_id", name="uq_workflow_preview_artifacts_run_id"),
    )
    for name, columns in (
        ("ix_workflow_preview_artifacts_tenant_id", ["tenant_id"]),
        ("ix_workflow_preview_artifacts_agent_id", ["agent_id"]),
        ("ix_workflow_preview_artifacts_session_id", ["session_id"]),
        ("ix_workflow_preview_artifacts_requested_by_user_id", ["requested_by_user_id"]),
        ("ix_workflow_preview_artifacts_proposal_id", ["proposal_id"]),
        ("ix_workflow_preview_artifacts_expires_at", ["expires_at"]),
        ("ix_workflow_preview_identity", ["tenant_id", "agent_id", "session_id", "requested_by_user_id"]),
        ("ix_workflow_preview_start_claim", ["status", "claim_expires_at"]),
    ):
        create_index_if_missing(op, name, "workflow_preview_artifacts", columns)

    for table in _TABLES:
        _enable_rls(table)


def downgrade() -> None:
    op.drop_table("workflow_preview_artifacts")
    op.drop_table("workflow_proposal_artifacts")
