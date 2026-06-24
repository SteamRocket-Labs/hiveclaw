"""Add governed A2A collaboration groups.

Revision ID: a2a_collaboration_groups_0624
Revises: local_agent_channel_0622
Create Date: 2026-06-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a2a_collaboration_groups_0624"
down_revision = "local_agent_channel_0622"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_collaboration_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_by_agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default="group_members"),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_collaboration_groups_tenant_id", "agent_collaboration_groups", ["tenant_id"])
    op.create_index(
        "ix_agent_collaboration_groups_tenant_status",
        "agent_collaboration_groups",
        ["tenant_id", "status"],
    )
    op.create_index("ix_agent_collaboration_groups_status", "agent_collaboration_groups", ["status"])
    op.create_index(
        "ix_agent_collaboration_groups_created_by_agent_id",
        "agent_collaboration_groups",
        ["created_by_agent_id"],
    )
    op.create_index("ix_agent_collaboration_groups_created_at", "agent_collaboration_groups", ["created_at"])

    op.create_table(
        "agent_collaboration_group_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_collaboration_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("agent_owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="member"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="pending_owner_confirmation"),
        sa.Column("invited_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("invited_by_agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("capability_scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("invitation_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("group_id", "agent_id", name="uq_agent_collaboration_group_member_agent"),
    )
    op.create_index("ix_agent_collaboration_group_members_tenant_id", "agent_collaboration_group_members", ["tenant_id"])
    op.create_index("ix_agent_collaboration_group_members_group_id", "agent_collaboration_group_members", ["group_id"])
    op.create_index("ix_agent_collaboration_group_members_agent_id", "agent_collaboration_group_members", ["agent_id"])
    op.create_index("ix_agent_collaboration_group_members_status", "agent_collaboration_group_members", ["status"])
    op.create_index(
        "ix_agent_collaboration_members_agent_status",
        "agent_collaboration_group_members",
        ["agent_id", "status"],
    )
    op.create_index(
        "ix_agent_collaboration_members_owner_status",
        "agent_collaboration_group_members",
        ["agent_owner_user_id", "status"],
    )
    op.create_index(
        "ix_agent_collaboration_members_tenant_group",
        "agent_collaboration_group_members",
        ["tenant_id", "group_id"],
    )
    op.create_index(
        "ix_agent_collaboration_group_members_invited_by_user_id",
        "agent_collaboration_group_members",
        ["invited_by_user_id"],
    )
    op.create_index(
        "ix_agent_collaboration_group_members_invited_by_agent_id",
        "agent_collaboration_group_members",
        ["invited_by_agent_id"],
    )
    op.create_index(
        "ix_agent_collaboration_group_members_approved_by_user_id",
        "agent_collaboration_group_members",
        ["approved_by_user_id"],
    )
    op.create_index("ix_agent_collaboration_group_members_created_at", "agent_collaboration_group_members", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_collaboration_group_members_created_at", table_name="agent_collaboration_group_members")
    op.drop_index("ix_agent_collaboration_group_members_approved_by_user_id", table_name="agent_collaboration_group_members")
    op.drop_index("ix_agent_collaboration_group_members_invited_by_agent_id", table_name="agent_collaboration_group_members")
    op.drop_index("ix_agent_collaboration_group_members_invited_by_user_id", table_name="agent_collaboration_group_members")
    op.drop_index("ix_agent_collaboration_members_tenant_group", table_name="agent_collaboration_group_members")
    op.drop_index("ix_agent_collaboration_members_owner_status", table_name="agent_collaboration_group_members")
    op.drop_index("ix_agent_collaboration_members_agent_status", table_name="agent_collaboration_group_members")
    op.drop_index("ix_agent_collaboration_group_members_status", table_name="agent_collaboration_group_members")
    op.drop_index("ix_agent_collaboration_group_members_agent_id", table_name="agent_collaboration_group_members")
    op.drop_index("ix_agent_collaboration_group_members_group_id", table_name="agent_collaboration_group_members")
    op.drop_index("ix_agent_collaboration_group_members_tenant_id", table_name="agent_collaboration_group_members")
    op.drop_table("agent_collaboration_group_members")

    op.drop_index("ix_agent_collaboration_groups_created_at", table_name="agent_collaboration_groups")
    op.drop_index("ix_agent_collaboration_groups_created_by_agent_id", table_name="agent_collaboration_groups")
    op.drop_index("ix_agent_collaboration_groups_status", table_name="agent_collaboration_groups")
    op.drop_index("ix_agent_collaboration_groups_tenant_status", table_name="agent_collaboration_groups")
    op.drop_index("ix_agent_collaboration_groups_tenant_id", table_name="agent_collaboration_groups")
    op.drop_table("agent_collaboration_groups")
