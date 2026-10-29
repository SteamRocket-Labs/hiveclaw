"""Separate original content deduplication from immutable physical generations."""

from alembic import op
import sqlalchemy as sa

from app.migration_compat import create_index_if_missing

revision = "original_blob_generations_1021"
down_revision = "chat_snapshot_payload_refs_1021"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("uq_storage_blobs_tenant_digest", "storage_blobs", type_="unique", if_exists=True)
    create_index_if_missing(op, "uq_storage_blobs_active_digest", "storage_blobs", ["tenant_id", "content_sha256"], unique=True,
        postgresql_where=sa.text("state IN ('uploading','available')"))


def downgrade_preflight():
    connection = op.get_bind()
    previous = connection.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    try:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
        populated = connection.scalar(sa.text("""
            SELECT EXISTS(SELECT 1 FROM storage_blobs WHERE kind='chat_upload'
                AND object_key <> tenant_id::text || '/' || content_sha256)
            OR EXISTS(SELECT 1 FROM storage_blobs GROUP BY tenant_id,content_sha256 HAVING count(*)>1)
        """))
        if populated:
            raise RuntimeError("original_generations_require_recovery_before_downgrade")
    finally:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"), {"scope": previous or ""})


def downgrade():
    downgrade_preflight()
    op.drop_index("uq_storage_blobs_active_digest", table_name="storage_blobs")
    op.create_unique_constraint("uq_storage_blobs_tenant_digest", "storage_blobs", ["tenant_id", "content_sha256"])
