"""Bind explicit memory versions to their authenticated source audience."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.migration_compat import add_column_if_missing

revision = "explicit_memory_sources_1021"
down_revision = "company_knowledge_analysis_1020"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing(
        op,
        "content_input_receipts",
        sa.Column("authority_json", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.drop_constraint("ck_content_input_receipts_carrier_kind", "content_input_receipts", type_="check")
    op.create_check_constraint(
        "ck_content_input_receipts_carrier_kind",
        "content_input_receipts",
        "carrier_kind IN ('team_memory','workspace_artifact','explicit_memory')",
    )


def downgrade():
    connection = op.get_bind()
    previous = connection.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    try:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
        populated = connection.scalar(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM content_input_receipts "
                "WHERE carrier_kind='explicit_memory' OR authority_json <> '{}'::jsonb)"
            )
        )
        if populated:
            raise RuntimeError("explicit_memory_source_receipts_require_recovery_before_downgrade")
    finally:
        connection.execute(
            sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"), {"scope": previous or ""}
        )
    op.drop_constraint("ck_content_input_receipts_carrier_kind", "content_input_receipts", type_="check")
    op.create_check_constraint(
        "ck_content_input_receipts_carrier_kind",
        "content_input_receipts",
        "carrier_kind IN ('team_memory','workspace_artifact')",
    )
    op.drop_column("content_input_receipts", "authority_json")
