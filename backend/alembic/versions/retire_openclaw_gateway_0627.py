"""Retire legacy OpenClaw gateway surface.

Revision ID: retire_openclaw_gateway_0627
Revises: a2a_collaboration_groups_0624
Create Date: 2026-06-27 00:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "retire_openclaw_gateway_0627"
down_revision = "a2a_collaboration_groups_0624"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE agents SET agent_type = 'native' WHERE agent_type = 'openclaw'")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS api_key_hash")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS openclaw_last_seen")
    op.execute("DROP TABLE IF EXISTS gateway_messages")


def downgrade() -> None:
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(128)")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS openclaw_last_seen TIMESTAMPTZ")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_messages (
            id UUID PRIMARY KEY,
            agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
            sender_agent_id UUID REFERENCES agents(id) ON DELETE SET NULL,
            sender_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            conversation_id VARCHAR(255),
            client_message_id VARCHAR(128),
            content TEXT NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            result TEXT,
            attachments_json JSON,
            metadata_json JSON,
            delivered_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_gateway_messages_agent_id ON gateway_messages (agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_gateway_messages_tenant_id ON gateway_messages (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_gateway_messages_status ON gateway_messages (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_gateway_messages_client_message_id ON gateway_messages (client_message_id)")
