"""Add restart-resumable Company Knowledge import jobs.

Revision ID: company_knowledge_runtime_0724
Revises: company_knowledge_closed_loop_0724
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "company_knowledge_runtime_0724"
down_revision: str | None = "company_knowledge_closed_loop_0724"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "company_knowledge_import_jobs"


def _enable_strict_rls() -> None:
    predicate = (
        "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid "
        "OR current_setting('app.rls_bypass', true) = 'on'"
    )
    op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(f'DROP POLICY IF EXISTS "tenant_isolation_{TABLE}" ON "{TABLE}"')
    op.execute(f'CREATE POLICY "tenant_isolation_{TABLE}" ON "{TABLE}" USING ({predicate}) WITH CHECK ({predicate})')


def upgrade() -> None:
    op.add_column(
        "company_knowledge_events",
        sa.Column("stream_sequence", sa.BigInteger(), nullable=True),
    )
    op.execute(
        """
        WITH sequenced AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY tenant_id
                    ORDER BY created_at, id
                ) AS stream_sequence
            FROM company_knowledge_events
        )
        UPDATE company_knowledge_events AS events
        SET stream_sequence = sequenced.stream_sequence
        FROM sequenced
        WHERE events.id = sequenced.id
        """
    )
    op.alter_column("company_knowledge_events", "stream_sequence", nullable=False)
    op.create_unique_constraint(
        "uq_company_knowledge_event_stream_sequence",
        "company_knowledge_events",
        ["tenant_id", "stream_sequence"],
    )
    op.create_index(
        "ix_company_knowledge_event_tenant_sequence",
        "company_knowledge_events",
        ["tenant_id", "stream_sequence"],
        unique=False,
    )

    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("source_contract_id", sa.UUID(), nullable=False),
        sa.Column("source_contract_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("request_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("artifact_ref", sa.String(length=1000), nullable=True),
        sa.Column("artifact_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", sa.UUID(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("last_error_code", sa.String(length=120), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("evidence_id", sa.UUID(), nullable=True),
        sa.Column("document_id", sa.UUID(), nullable=True),
        sa.Column("created_by_type", sa.String(length=30), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=False),
        sa.Column("accountable_user_id", sa.UUID(), nullable=False),
        sa.Column("trace_id", sa.String(length=300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','held','failed','cancelled')",
            name="ck_company_knowledge_import_job_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="ck_company_knowledge_import_job_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["accountable_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["company_knowledge_evidence.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_contract_id"],
            ["company_knowledge_source_contracts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["company_knowledge_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_company_knowledge_import_job_idempotency",
        ),
    )
    op.create_index(
        "ix_company_knowledge_import_job_claim",
        TABLE,
        ["tenant_id", "status", "available_at", "created_at"],
        unique=False,
    )
    for column in ("document_id", "evidence_id", "source_contract_id", "source_id", "status", "tenant_id"):
        op.create_index(f"ix_{TABLE}_{column}", TABLE, [column], unique=False)
    _enable_strict_rls()


def downgrade() -> None:
    op.drop_index("ix_company_knowledge_import_job_claim", table_name=TABLE)
    for column in ("document_id", "evidence_id", "source_contract_id", "source_id", "status", "tenant_id"):
        op.drop_index(f"ix_{TABLE}_{column}", table_name=TABLE)
    op.drop_table(TABLE)
    op.drop_index(
        "ix_company_knowledge_event_tenant_sequence",
        table_name="company_knowledge_events",
    )
    op.drop_constraint(
        "uq_company_knowledge_event_stream_sequence",
        "company_knowledge_events",
        type_="unique",
    )
    op.drop_column("company_knowledge_events", "stream_sequence")
