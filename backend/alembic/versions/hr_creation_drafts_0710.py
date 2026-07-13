"""Add canonical HR creation drafts and idempotent provisioning state.

Revision ID: hr_creation_drafts_0710
Revises: typed_thread_items_0710
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.migration_compat import create_index_if_missing, create_table_if_missing


revision = "hr_creation_drafts_0710"
down_revision = "typed_thread_items_0710"
branch_labels = None
depends_on = None

_HR_CREATION_TABLES = ("hr_creation_drafts",)


def upgrade() -> None:
    create_table_if_missing(
        op,
        "hr_creation_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "hr_agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
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
        sa.Column("status", sa.String(length=32), server_default="awaiting_confirmation", nullable=False),
        sa.Column("blueprint_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("blueprint_hash", sa.String(length=80), nullable=False),
        sa.Column("blueprint_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("preview_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "confirmed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "rejected_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("creation_idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provisioning_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "creation_idempotency_key", name="uq_hr_creation_draft_idempotency"),
        sa.CheckConstraint(
            "status IN ('awaiting_confirmation','confirmed','creating','provisioning','completed','failed','rejected','superseded','expired')",
            name="ck_hr_creation_draft_status",
        ),
    )
    for name, columns in (
        ("ix_hr_creation_drafts_tenant_id", ["tenant_id"]),
        ("ix_hr_creation_drafts_hr_agent_id", ["hr_agent_id"]),
        ("ix_hr_creation_drafts_session_id", ["session_id"]),
        ("ix_hr_creation_drafts_requested_by_user_id", ["requested_by_user_id"]),
        ("ix_hr_creation_drafts_created_agent_id", ["created_agent_id"]),
        ("ix_hr_creation_drafts_session_created", ["session_id", "created_at"]),
        ("ix_hr_creation_drafts_requester_status", ["requested_by_user_id", "status"]),
        ("ix_hr_creation_drafts_hr_status", ["hr_agent_id", "status"]),
    ):
        create_index_if_missing(op, name, "hr_creation_drafts", columns)

    op.execute("ALTER TABLE hr_creation_drafts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hr_creation_drafts FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_hr_creation_drafts ON hr_creation_drafts")
    op.execute(
        """
        CREATE POLICY tenant_isolation_hr_creation_drafts ON hr_creation_drafts
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


def downgrade() -> None:
    for name in (
        "ix_hr_creation_drafts_hr_status",
        "ix_hr_creation_drafts_requester_status",
        "ix_hr_creation_drafts_session_created",
        "ix_hr_creation_drafts_created_agent_id",
        "ix_hr_creation_drafts_requested_by_user_id",
        "ix_hr_creation_drafts_session_id",
        "ix_hr_creation_drafts_hr_agent_id",
        "ix_hr_creation_drafts_tenant_id",
    ):
        op.drop_index(name, table_name="hr_creation_drafts")
    op.drop_table("hr_creation_drafts")
