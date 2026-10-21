"""Mark the reader boundary for shared immutable snapshot payloads."""

from alembic import op
import sqlalchemy as sa

revision = "chat_snapshot_payload_refs_1021"
down_revision = "explicit_memory_sources_1021"
branch_labels = None
depends_on = None


def upgrade():
    # The version gate prevents older readers from collecting shared payloads.
    pass


def downgrade():
    connection = op.get_bind()
    previous = connection.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    try:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
        populated = connection.scalar(sa.text("""
            SELECT EXISTS(SELECT 1 FROM chat_artifacts
                WHERE snapshot_json->>'snapshot_payload_schema' = 'hive.chat_artifact_snapshot_payload.v1')
            OR EXISTS(SELECT 1 FROM chat_transcript_events WHERE actor_type <> 'user' AND (
                jsonb_path_exists(parts_json, :reference) OR jsonb_path_exists(metadata_json, :reference)))
            OR EXISTS(SELECT 1 FROM runtime_tasks WHERE jsonb_path_exists(metadata_json::jsonb, :reference))
        """), {"reference": '$.** ? (@.type == "artifact" && @.snapshot_payload_schema == "hive.chat_artifact_snapshot_payload.v1")'})
        if populated:
            raise RuntimeError("chat_snapshot_payloads_require_recovery_before_downgrade")
    finally:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"),
            {"scope": previous or ""})
