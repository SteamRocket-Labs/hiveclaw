"""Bind agent identity to sponsor and participant lifecycle.

Revision ID: agent_identity_lifecycle_0613
Revises: chat_message_thinking_signature_0613
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op

revision: str = "agent_identity_lifecycle_0613"
down_revision: Union[str, None] = "chat_message_thinking_signature_0613"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS sponsor_user_id UUID")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS participant_id UUID")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS deactivation_reason TEXT")
    op.execute(
        """
        INSERT INTO participants (id, type, ref_id, display_name, avatar_url)
        SELECT gen_random_uuid(), 'agent', a.id, LEFT(COALESCE(NULLIF(a.name, ''), 'Agent'), 100), a.avatar_url
        FROM agents AS a
        ON CONFLICT (type, ref_id) DO UPDATE
            SET display_name = EXCLUDED.display_name,
                avatar_url = EXCLUDED.avatar_url
        """
    )
    op.execute(
        """
        UPDATE agents AS a
        SET sponsor_user_id = COALESCE(a.owner_user_id, a.creator_id),
            participant_id = p.id
        FROM participants AS p
        WHERE p.type = 'agent'
          AND p.ref_id = a.id
          AND (a.sponsor_user_id IS NULL OR a.participant_id IS NULL)
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE agents
                ADD CONSTRAINT fk_agents_sponsor_user_id
                FOREIGN KEY (sponsor_user_id) REFERENCES users(id);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE agents
                ADD CONSTRAINT fk_agents_participant_id
                FOREIGN KEY (participant_id) REFERENCES participants(id);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
        """
    )
    op.execute("ALTER TABLE agents ALTER COLUMN sponsor_user_id SET NOT NULL")
    op.execute("ALTER TABLE agents ALTER COLUMN participant_id SET NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agents_sponsor_user_id ON agents (sponsor_user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agents_participant_id ON agents (participant_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agents_active_lifecycle "
        "ON agents (tenant_id, status) WHERE deleted_at IS NULL AND deactivated_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agents_active_lifecycle")
    op.execute("DROP INDEX IF EXISTS ix_agents_participant_id")
    op.execute("DROP INDEX IF EXISTS ix_agents_sponsor_user_id")
    op.execute("ALTER TABLE agents DROP CONSTRAINT IF EXISTS fk_agents_participant_id")
    op.execute("ALTER TABLE agents DROP CONSTRAINT IF EXISTS fk_agents_sponsor_user_id")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS deactivation_reason")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS deactivated_at")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS participant_id")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS sponsor_user_id")
