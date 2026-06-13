"""Add append-only session feedback events.

Revision ID: session_feedback_events_0613
Revises: web_chat_active_run_unique_0612
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op

revision: str = "session_feedback_events_0613"
down_revision: Union[str, None] = "web_chat_active_run_unique_0612"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS session_feedback_events (
            id UUID PRIMARY KEY,
            tenant_id UUID REFERENCES tenants(id),
            agent_id UUID NOT NULL REFERENCES agents(id),
            session_id UUID NOT NULL REFERENCES chat_sessions(id),
            message_id UUID REFERENCES chat_messages(id),
            user_id UUID NOT NULL REFERENCES users(id),
            label VARCHAR(20) NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            attribution_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            calibration_result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            CONSTRAINT ck_session_feedback_events_label CHECK (label IN ('useful', 'misleading'))
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_session_feedback_events_tenant_id ON session_feedback_events (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_session_feedback_events_agent_id ON session_feedback_events (agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_session_feedback_events_session_id ON session_feedback_events (session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_session_feedback_events_user_id ON session_feedback_events (user_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_feedback_events_created_at ON session_feedback_events (created_at)"
    )
    op.execute("ALTER TABLE session_feedback_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE session_feedback_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $$ BEGIN
            CREATE POLICY tenant_isolation_session_feedback_events ON session_feedback_events
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
    op.execute("DROP POLICY IF EXISTS tenant_isolation_session_feedback_events ON session_feedback_events")
    op.execute("DROP TABLE IF EXISTS session_feedback_events")
