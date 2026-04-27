"""Add agent objective ledger.

Revision ID: add_agent_objectives_0427
Revises: add_sso_scan_sessions_updated_at_0419
Create Date: 2026-04-27
"""

from typing import Sequence, Union

from alembic import op


revision: str = "add_agent_objectives_0427"
down_revision: Union[str, None] = "add_sso_scan_sessions_updated_at_0419"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_objectives (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            tenant_id UUID REFERENCES tenants(id),
            objective_key VARCHAR(200) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status VARCHAR(30) NOT NULL DEFAULT 'open',
            priority INTEGER NOT NULL DEFAULT 0,
            source VARCHAR(50) NOT NULL DEFAULT 'focus_md',
            success_criteria TEXT,
            blocked_reason TEXT,
            metadata_json JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT uq_agent_objective_key UNIQUE (agent_id, objective_key)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_objectives_agent_id ON agent_objectives (agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_objectives_tenant_id ON agent_objectives (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_objectives_status ON agent_objectives (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_objectives_created_at ON agent_objectives (created_at)")

    op.execute("ALTER TABLE agent_objectives ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $$ BEGIN
            CREATE POLICY tenant_isolation_agent_objectives ON agent_objectives
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
    op.execute("DROP POLICY IF EXISTS tenant_isolation_agent_objectives ON agent_objectives")
    op.drop_index("ix_agent_objectives_created_at", table_name="agent_objectives")
    op.drop_index("ix_agent_objectives_status", table_name="agent_objectives")
    op.drop_index("ix_agent_objectives_tenant_id", table_name="agent_objectives")
    op.drop_index("ix_agent_objectives_agent_id", table_name="agent_objectives")
    op.drop_table("agent_objectives")
