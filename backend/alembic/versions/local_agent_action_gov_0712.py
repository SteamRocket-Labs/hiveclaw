"""Govern Local Agent actions, delivery recovery, and bridge token lifetime.

Revision ID: local_agent_action_gov_0712
Revises: channel_secret_encryption_0712
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "local_agent_action_gov_0712"
down_revision = "channel_secret_encryption_0712"
branch_labels = None
depends_on = None


LOCAL_AGENT_POLICY_SEED = "local_agent_action_gov_0712"


def upgrade() -> None:
    op.add_column(
        "local_agent_channel_messages",
        sa.Column(
            "approval_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approval_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "local_agent_channel_messages",
        sa.Column("delivery_attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "local_agent_channel_messages",
        sa.Column("delivery_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_local_agent_channel_messages_approval_id",
        "local_agent_channel_messages",
        ["approval_id"],
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE local_agent_channel_messages
            SET delivery_attempt_count = CASE WHEN status = 'delivered' THEN 1 ELSE 0 END,
                delivery_lease_expires_at = CASE WHEN status = 'delivered' THEN now() ELSE NULL END
            """
        )
    )
    # Legacy permanent credentials receive a bounded grace window. New
    # credentials always get a 30-day expiry at exchange time.
    bind.execute(
        sa.text(
            """
            UPDATE local_agent_bridge_connections
            SET expires_at = GREATEST(created_at + interval '30 days', now() + interval '7 days')
            WHERE status = 'active' AND expires_at IS NULL
            """
        )
    )
    # Missing action policy is deny at runtime. Existing local agents receive
    # explicit per-agent defaults so the migration does not strand them:
    # execute/file actions remain blocked until a one-time owner decision;
    # event/result capabilities only transport evidence for an approved run.
    bind.execute(
        sa.text(
            """
            INSERT INTO capability_policies
                (id, tenant_id, agent_id, capability, allowed, requires_approval, conditions)
            SELECT
                gen_random_uuid(),
                agent.tenant_id,
                agent.id,
                seed.capability,
                true,
                seed.requires_approval,
                jsonb_build_object(
                    'seeded_by', CAST(:seeded_by AS text),
                    'action_default',
                    CASE WHEN seed.requires_approval THEN 'require_owner_approval' ELSE 'protocol_receipt' END
                )
            FROM agents AS agent
            CROSS JOIN (
                VALUES
                    ('local_agent.execute', true),
                    ('local_agent.file_download', true),
                    ('local_agent.file_upload', true),
                    ('local_agent.event_stream', false),
                    ('local_agent.result_report', false)
            ) AS seed(capability, requires_approval)
            WHERE agent.agent_type = 'local_agent'
              AND agent.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM capability_policies AS existing
                  WHERE existing.tenant_id = agent.tenant_id
                    AND existing.agent_id = agent.id
                    AND existing.capability = seed.capability
              )
            """
        ),
        {"seeded_by": LOCAL_AGENT_POLICY_SEED},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM capability_policies
            WHERE conditions ->> 'seeded_by' = :seeded_by
            """
        ),
        {"seeded_by": LOCAL_AGENT_POLICY_SEED},
    )
    op.drop_index("ix_local_agent_channel_messages_approval_id", table_name="local_agent_channel_messages")
    op.drop_column("local_agent_channel_messages", "delivery_lease_expires_at")
    op.drop_column("local_agent_channel_messages", "delivery_attempt_count")
    op.drop_column("local_agent_channel_messages", "approval_id")
