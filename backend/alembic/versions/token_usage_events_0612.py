"""Add append-only token usage events.

O3 closes the metering blind spot for autonomous LLM paths and fixes the admin
time-series proxy that previously attributed all historical usage to the agent
creation date. The table is append-only evidence; existing Agent/User aggregate
counters remain for quota and leaderboard reads.

Revision ID: token_usage_events_0612
Revises: rls_stage2c_drop_orphan_tables_0611
Create Date: 2026-06-12
"""

from typing import Sequence, Union

from alembic import op

revision: str = "token_usage_events_0612"
down_revision: Union[str, None] = "rls_stage2c_drop_orphan_tables_0611"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS token_usage_events (
            id UUID PRIMARY KEY,
            tenant_id UUID REFERENCES tenants(id),
            agent_id UUID REFERENCES agents(id),
            user_id UUID REFERENCES users(id),
            source VARCHAR(100) NOT NULL DEFAULT 'unknown',
            provider VARCHAR(50),
            model VARCHAR(200),
            tokens INTEGER NOT NULL DEFAULT 0,
            usage JSONB,
            details JSONB,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_events_tenant_id ON token_usage_events (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_events_agent_id ON token_usage_events (agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_events_user_id ON token_usage_events (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_events_source ON token_usage_events (source)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_events_created_at ON token_usage_events (created_at)")
    op.execute("ALTER TABLE token_usage_events ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $$ BEGIN
            CREATE POLICY tenant_isolation_token_usage_events ON token_usage_events
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
    op.execute("DROP POLICY IF EXISTS tenant_isolation_token_usage_events ON token_usage_events")
    op.execute("DROP TABLE IF EXISTS token_usage_events")
