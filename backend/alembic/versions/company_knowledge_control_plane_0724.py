"""Bind Company Knowledge review to materialized content.

Revision ID: company_knowledge_control_plane_0724
Revises: company_ontology_runtime_0724
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "company_knowledge_control_plane_0724"
down_revision = "company_ontology_runtime_0724"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "company_knowledge_proposals",
        sa.Column("materialized_document_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "company_knowledge_proposals",
        sa.Column("materialization_content_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "company_knowledge_proposals",
        sa.Column(
            "materialization_receipt_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "company_knowledge_proposals",
        sa.Column("materialization_idempotency_key", sa.String(length=300), nullable=True),
    )
    op.add_column(
        "company_knowledge_proposals",
        sa.Column("materialized_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "company_knowledge_proposals",
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_company_knowledge_proposal_materialized_document",
        "company_knowledge_proposals",
        "knowledge_documents",
        ["materialized_document_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_company_knowledge_proposal_materialized_by",
        "company_knowledge_proposals",
        "users",
        ["materialized_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_company_knowledge_proposals_materialized_document_id",
        "company_knowledge_proposals",
        ["materialized_document_id"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_company_knowledge_proposal_materialization_idempotency",
        "company_knowledge_proposals",
        ["tenant_id", "materialization_idempotency_key"],
    )

    op.add_column(
        "company_knowledge_reviews",
        sa.Column("subject_content_hash", sa.String(length=64), nullable=True),
    )
    op.execute(
        """
        UPDATE company_knowledge_reviews AS review
        SET subject_content_hash = proposal.proposed_content_hash
        FROM company_knowledge_proposals AS proposal
        WHERE proposal.id = review.proposal_id
          AND proposal.tenant_id = review.tenant_id
          AND review.subject_content_hash IS NULL
        """
    )
    op.alter_column(
        "company_knowledge_reviews",
        "subject_content_hash",
        existing_type=sa.String(length=64),
        nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    materialized_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM company_knowledge_proposals "
            "WHERE materialized_document_id IS NOT NULL OR materialization_content_hash IS NOT NULL"
        )
    ).scalar_one()
    if materialized_count:
        raise RuntimeError("cannot downgrade Company Knowledge control plane while materialized proposals exist")

    op.drop_column("company_knowledge_reviews", "subject_content_hash")
    op.drop_constraint(
        "uq_company_knowledge_proposal_materialization_idempotency",
        "company_knowledge_proposals",
        type_="unique",
    )
    op.drop_index(
        "ix_company_knowledge_proposals_materialized_document_id",
        table_name="company_knowledge_proposals",
    )
    op.drop_constraint(
        "fk_company_knowledge_proposal_materialized_by",
        "company_knowledge_proposals",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_company_knowledge_proposal_materialized_document",
        "company_knowledge_proposals",
        type_="foreignkey",
    )
    op.drop_column("company_knowledge_proposals", "materialized_at")
    op.drop_column("company_knowledge_proposals", "materialized_by_user_id")
    op.drop_column("company_knowledge_proposals", "materialization_idempotency_key")
    op.drop_column("company_knowledge_proposals", "materialization_receipt_json")
    op.drop_column("company_knowledge_proposals", "materialization_content_hash")
    op.drop_column("company_knowledge_proposals", "materialized_document_id")
