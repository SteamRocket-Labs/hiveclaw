"""Persist evidence analysis and publication-bound ontology projections."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.migration_compat import create_table_if_missing, create_index_if_missing

revision = "company_knowledge_analysis_1020"
down_revision = "remote_a2a_runtime_1020"
branch_labels = None
depends_on = None

TABLES = ("company_knowledge_analysis_jobs", "company_knowledge_extractions")


def upgrade():
    connection = op.get_bind()
    previous_scope = connection.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    connection.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
    # Existing human review rounds retain their original subject hashes. New
    # submissions cannot supply this migration-owned compatibility receipt.
    op.execute("""UPDATE company_knowledge_proposals SET ontology_mapping_json =
        COALESCE(ontology_mapping_json, '{}'::jsonb) || jsonb_build_object('analysis_legacy_review',
            jsonb_build_object('revision', 'company_knowledge_analysis_1020',
                'candidate_hash', proposed_content_hash, 'materialized_hash', materialization_content_hash))
        WHERE proposal_kind <> 'ontology' AND status IN ('in_review','approved','published')""")
    connection.execute(
        sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"), {"scope": previous_scope or ""}
    )
    create_table_if_missing(
        op,
        TABLES[0],
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "evidence_id",
            UUID(as_uuid=True),
            sa.ForeignKey("company_knowledge_evidence.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("knowledge_documents.id", ondelete="RESTRICT")),
        sa.Column("namespace", sa.String(300), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request_json", JSONB, nullable=False),
        sa.Column(
            "accountable_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("claim_token", UUID(as_uuid=True)),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("error_code", sa.String(120)),
        sa.Column("dispatch_receipt_json", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "request_hash", name="uq_company_analysis_request"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','held','failed','cancelled')", name="ck_company_analysis_status"
        ),
        sa.CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_company_analysis_attempts"),
    )
    create_table_if_missing(
        op,
        TABLES[1],
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey(TABLES[0] + ".id", ondelete="RESTRICT"), nullable=False),
        sa.Column(
            "evidence_id",
            UUID(as_uuid=True),
            sa.ForeignKey("company_knowledge_evidence.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("validated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("candidate_json", JSONB, nullable=False),
        sa.Column("model_receipt_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "job_id", "attempt", name="uq_company_extraction_attempt"),
    )
    create_index_if_missing(
        op,
        "uq_company_extraction_accepted",
        TABLES[1],
        ["tenant_id", "job_id"],
        unique=True,
        postgresql_where=sa.text("validated = true"),
    )
    for table, fields in (
        (TABLES[0], ("tenant_id", "evidence_id")),
        (TABLES[1], ("tenant_id", "job_id", "evidence_id")),
    ):
        for field in fields:
            create_index_if_missing(op, f"ix_{table}_{field}", table, [field])
        predicate = (
            "current_setting('app.current_tenant_id', true) = 'BYPASS' "
            "OR tenant_id::text = current_setting('app.current_tenant_id', true)"
        )
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "tenant_isolation_{table}" ON "{table}" USING ({predicate}) WITH CHECK ({predicate})'
        )
    create_index_if_missing(op, "ix_company_analysis_claim", TABLES[0], ["tenant_id", "status", "available_at"])
    op.add_column(
        "company_knowledge_publications",
        sa.Column("graph_projection_status", sa.String(40), nullable=False, server_default="not_analyzed"),
    )
    op.add_column(
        "company_knowledge_publications",
        sa.Column("graph_projection_receipt_json", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column(
        "company_knowledge_publications",
        sa.Column("analysis_extraction_id", UUID(as_uuid=True), sa.ForeignKey(TABLES[1] + ".id", ondelete="RESTRICT")),
    )
    op.add_column(
        "company_ontology_releases",
        sa.Column(
            "publication_id",
            UUID(as_uuid=True),
            sa.ForeignKey("company_knowledge_publications.id", ondelete="RESTRICT"),
        ),
    )
    create_index_if_missing(
        op, "ix_company_ontology_releases_publication_id", "company_ontology_releases", ["publication_id"]
    )
    op.drop_index("ix_company_ontology_release_active", table_name="company_ontology_releases")
    create_index_if_missing(
        op,
        "ix_company_ontology_release_active",
        "company_ontology_releases",
        ["tenant_id", "namespace"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND publication_id IS NULL"),
    )
    create_index_if_missing(
        op,
        "ix_company_ontology_publication_active",
        "company_ontology_releases",
        ["tenant_id", "publication_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND publication_id IS NOT NULL"),
    )


def downgrade():
    connection = op.get_bind()
    previous_scope = connection.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    connection.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
    populated = any(
        connection.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}")')).scalar() for table in TABLES
    )
    projections = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM company_ontology_releases WHERE publication_id IS NOT NULL)")
    ).scalar()
    connection.execute(
        sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"), {"scope": previous_scope or ""}
    )
    if populated:
        raise RuntimeError("company_knowledge_analysis_downgrade_requires_empty_runtime_data")
    if projections:
        raise RuntimeError("company_knowledge_analysis_downgrade_requires_empty_projections")
    op.drop_index("ix_company_ontology_publication_active", table_name="company_ontology_releases")
    op.drop_index("ix_company_ontology_release_active", table_name="company_ontology_releases")
    op.create_index(
        "ix_company_ontology_release_active",
        "company_ontology_releases",
        ["tenant_id", "namespace"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index("ix_company_ontology_releases_publication_id", table_name="company_ontology_releases")
    op.drop_column("company_ontology_releases", "publication_id")
    op.drop_column("company_knowledge_publications", "analysis_extraction_id")
    op.drop_column("company_knowledge_publications", "graph_projection_status")
    op.drop_column("company_knowledge_publications", "graph_projection_receipt_json")
    for table in reversed(TABLES):
        op.drop_table(table)
