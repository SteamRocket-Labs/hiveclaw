"""Add immutable two-person Workflow promotion proposals.

Revision ID: workflow_promotion_proposals_0711
Revises: resource_authority_0711
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.migration_compat import (
    add_column_if_missing,
    create_foreign_key_if_missing,
    create_index_if_missing,
    create_table_if_missing,
    create_unique_constraint_if_missing,
)


revision = "workflow_promotion_proposals_0711"
down_revision = "resource_authority_0711"
branch_labels = None
depends_on = None

_WORKFLOW_PROMOTION_TABLES = ("workflow_promotion_proposals",)


def upgrade() -> None:
    create_table_if_missing(
        op,
        "workflow_promotion_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "root_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "requester_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("proposal_hash", sa.String(length=64), nullable=False),
        sa.Column("run_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("definition_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("run_evidence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','stale','withdrawn')",
            name="ck_workflow_promotion_proposal_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "proposal_hash",
            name="uq_workflow_promotion_proposal_hash",
        ),
    )
    for column in (
        "tenant_id",
        "agent_id",
        "run_id",
        "root_session_id",
        "requester_user_id",
        "reviewer_user_id",
        "status",
    ):
        create_index_if_missing(
            op,
            f"ix_workflow_promotion_proposals_{column}",
            "workflow_promotion_proposals",
            [column],
        )

    add_column_if_missing(
        op,
        "workflow_definitions",
        sa.Column("promotion_proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    create_foreign_key_if_missing(
        op,
        "fk_workflow_definitions_promotion_proposal_id",
        "workflow_definitions",
        "workflow_promotion_proposals",
        ["promotion_proposal_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    create_unique_constraint_if_missing(
        op,
        "uq_workflow_definition_promotion_proposal",
        "workflow_definitions",
        ["promotion_proposal_id"],
    )
    create_index_if_missing(
        op,
        "ix_workflow_definitions_promotion_proposal_id",
        "workflow_definitions",
        ["promotion_proposal_id"],
    )

    op.execute("ALTER TABLE workflow_promotion_proposals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workflow_promotion_proposals FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_workflow_promotion_proposals
        ON workflow_promotion_proposals
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            OR current_setting('app.rls_bypass', true) = 'on'
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            OR current_setting('app.rls_bypass', true) = 'on'
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION workflow_promotion_snapshot_immutable()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'workflow promotion proposal snapshots are immutable';
            END IF;
            IF OLD.status IS DISTINCT FROM NEW.status
               AND NOT (
                   (OLD.status = 'pending' AND NEW.status IN ('approved','rejected','stale','withdrawn'))
                   OR (OLD.status = 'withdrawn' AND NEW.status = 'pending')
               ) THEN
                RAISE EXCEPTION 'invalid workflow promotion proposal transition';
            END IF;
            IF OLD.status IN ('approved','rejected','stale')
               AND (
                   OLD.reviewer_user_id IS DISTINCT FROM NEW.reviewer_user_id
                   OR OLD.review_reason IS DISTINCT FROM NEW.review_reason
                   OR OLD.reviewed_at IS DISTINCT FROM NEW.reviewed_at
               ) THEN
                RAISE EXCEPTION 'terminal workflow promotion review is immutable';
            END IF;
            IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
               OR OLD.agent_id IS DISTINCT FROM NEW.agent_id
               OR OLD.run_id IS DISTINCT FROM NEW.run_id
               OR OLD.root_session_id IS DISTINCT FROM NEW.root_session_id
               OR OLD.requester_user_id IS DISTINCT FROM NEW.requester_user_id
               OR OLD.proposal_hash IS DISTINCT FROM NEW.proposal_hash
               OR OLD.run_evidence_hash IS DISTINCT FROM NEW.run_evidence_hash
               OR OLD.definition_hash IS DISTINCT FROM NEW.definition_hash
               OR OLD.definition_json IS DISTINCT FROM NEW.definition_json
               OR OLD.run_evidence_json IS DISTINCT FROM NEW.run_evidence_json
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'workflow promotion proposal snapshots are immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_workflow_promotion_snapshot_immutable
        BEFORE UPDATE OR DELETE ON workflow_promotion_proposals
        FOR EACH ROW EXECUTE FUNCTION workflow_promotion_snapshot_immutable()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_workflow_promotion_snapshot_immutable ON workflow_promotion_proposals")
    op.execute("DROP FUNCTION IF EXISTS workflow_promotion_snapshot_immutable()")
    op.drop_index("ix_workflow_definitions_promotion_proposal_id", table_name="workflow_definitions")
    op.drop_constraint(
        "uq_workflow_definition_promotion_proposal",
        "workflow_definitions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_workflow_definitions_promotion_proposal_id",
        "workflow_definitions",
        type_="foreignkey",
    )
    op.drop_column("workflow_definitions", "promotion_proposal_id")
    op.drop_table("workflow_promotion_proposals")
