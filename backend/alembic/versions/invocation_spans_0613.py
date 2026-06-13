"""Persist invocation spans for trace reader.

Revision ID: invocation_spans_0613
Revises: agent_identity_lifecycle_0613
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op

revision: str = "invocation_spans_0613"
down_revision: Union[str, None] = "agent_identity_lifecycle_0613"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS invocation_spans (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            agent_id UUID REFERENCES agents(id),
            user_id UUID REFERENCES users(id),
            runtime_task_id UUID REFERENCES runtime_tasks(id),
            session_id VARCHAR(255),
            request_id UUID,
            trace_id VARCHAR(255) NOT NULL,
            span_id VARCHAR(80) NOT NULL,
            parent_span_id VARCHAR(80),
            parent_trace_id VARCHAR(255),
            span_type VARCHAR(50) NOT NULL,
            name VARCHAR(200) NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'ok',
            started_at TIMESTAMP WITH TIME ZONE NOT NULL,
            ended_at TIMESTAMP WITH TIME ZONE,
            duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
            usage JSONB,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            error TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            CONSTRAINT uq_invocation_spans_trace_span UNIQUE (tenant_id, trace_id, span_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_invocation_spans_tenant_id ON invocation_spans (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_invocation_spans_agent_id ON invocation_spans (agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_invocation_spans_user_id ON invocation_spans (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_invocation_spans_session_id ON invocation_spans (session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_invocation_spans_span_type ON invocation_spans (span_type)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_invocation_spans_status ON invocation_spans (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_invocation_spans_created_at ON invocation_spans (created_at)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_invocation_spans_tenant_trace ON invocation_spans (tenant_id, trace_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_invocation_spans_parent_trace ON invocation_spans (tenant_id, parent_trace_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_invocation_spans_runtime_task_id ON invocation_spans (runtime_task_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_invocation_spans_request_id ON invocation_spans (request_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_invocation_spans_agent_started ON invocation_spans (agent_id, started_at)"
    )
    op.execute("ALTER TABLE invocation_spans ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invocation_spans FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $$ BEGIN
            CREATE POLICY tenant_isolation_invocation_spans ON invocation_spans
                USING (
                    current_setting('app.current_tenant_id', true) = 'BYPASS'
                    OR tenant_id::text = current_setting('app.current_tenant_id', true)
                );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_invocation_spans ON invocation_spans")
    op.execute("DROP TABLE IF EXISTS invocation_spans")
