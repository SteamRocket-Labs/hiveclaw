"""Link recoverable Company promotion intake to its submitted proposal.

Revision ID: company_knowledge_promotion_intake_0724
Revises: company_knowledge_control_plane_0724
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "company_knowledge_promotion_intake_0724"
down_revision = "company_knowledge_control_plane_0724"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "company_knowledge_import_jobs",
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_company_knowledge_import_job_proposal",
        "company_knowledge_import_jobs",
        "company_knowledge_proposals",
        ["proposal_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_company_knowledge_import_jobs_proposal_id",
        "company_knowledge_import_jobs",
        ["proposal_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    promotion_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM company_knowledge_import_jobs "
            "WHERE proposal_id IS NOT NULL OR request_json ? 'promotion_handoff'"
        )
    ).scalar_one()
    if promotion_count:
        raise RuntimeError("cannot downgrade Company Knowledge promotion intake while promotion handoffs exist")

    op.drop_index(
        "ix_company_knowledge_import_jobs_proposal_id",
        table_name="company_knowledge_import_jobs",
    )
    op.drop_constraint(
        "fk_company_knowledge_import_job_proposal",
        "company_knowledge_import_jobs",
        type_="foreignkey",
    )
    op.drop_column("company_knowledge_import_jobs", "proposal_id")
