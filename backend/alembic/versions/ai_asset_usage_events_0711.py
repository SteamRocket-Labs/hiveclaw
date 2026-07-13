"""Add durable revision-bound AI asset usage events.

Revision ID: ai_asset_usage_events_0711
Revises: hr_provisioning_steps_0711
Create Date: 2026-07-11
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.migration_compat import create_index_if_missing, create_table_if_missing


revision = "ai_asset_usage_events_0711"
down_revision = "hr_provisioning_steps_0711"
branch_labels = None
depends_on = None


def _canonical_hash(content: dict[str, Any]) -> str:
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _seed_workflow_asset(bind, row: Any) -> None:
    content = {
        "schema": "hive.ai_asset.workflow.v1",
        "asset_type": "workflow",
        "definition": row["definition_json"],
        "control": {
            "status": row["status"],
            "visibility_scope": row["visibility_scope"],
            "call_policy": row["call_policy"],
            "owner_type": row["owner_type"],
            "owner_id": str(row["owner_id"]) if row["owner_id"] else None,
        },
    }
    native_key = f"workflow:{row['name']}@{row['definition_version']}"
    asset_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    content_hash = _canonical_hash(content)
    inserted = bind.execute(
        sa.text(
            """
            INSERT INTO ai_asset_records (
                id, tenant_id, asset_type, native_entity_id, native_key, native_locator_json,
                display_name, owner_type, owner_id, visibility_scope, lifecycle_status,
                content_hash, source_type, source_ref, trust_state, dependencies_json,
                compatibility_json, admission_state, usage_count, usage_evidence_json,
                projection_status, created_by_user_id, created_by_agent_id, created_at, updated_at
            ) VALUES (
                :id, :tenant_id, 'workflow', :native_entity_id, :native_key, CAST(:locator AS jsonb),
                :display_name, :owner_type, :owner_id, :visibility_scope, :lifecycle_status,
                :content_hash, 'workflow_registry', :source_ref, 'trusted', CAST(:dependencies AS jsonb),
                '{}'::jsonb, 'admitted', 0, '[]'::jsonb, 'applied', :created_by_user_id,
                :created_by_agent_id, now(), now()
            )
            ON CONFLICT (tenant_id, asset_type, native_key) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": asset_id,
            "tenant_id": row["tenant_id"],
            "native_entity_id": row["id"],
            "native_key": native_key,
            "locator": _json({"name": row["name"], "version": row["definition_version"]}),
            "display_name": row["name"],
            "owner_type": row["owner_type"],
            "owner_id": row["owner_id"],
            "visibility_scope": row["visibility_scope"],
            "lifecycle_status": row["status"],
            "content_hash": content_hash,
            "source_ref": native_key,
            "dependencies": _json(list((row["definition_json"] or {}).get("dependencies") or [])),
            "created_by_user_id": row["created_by_user_id"],
            "created_by_agent_id": row["created_by_agent_id"],
        },
    ).scalar_one_or_none()
    if inserted is None:
        return
    bind.execute(
        sa.text(
            """
            INSERT INTO config_revisions (
                id, entity_type, entity_id, tenant_id, version, content_hash, content,
                diff_from_prev, change_source, change_message, is_active, created_at
            ) VALUES (
                :id, 'ai_asset', :asset_id, :tenant_id, 1, :content_hash,
                CAST(:content AS jsonb), CAST(:diff AS jsonb), 'migration',
                'Version-bound Workflow asset backfill', true, now()
            )
            """
        ),
        {
            "id": revision_id,
            "asset_id": asset_id,
            "tenant_id": row["tenant_id"],
            "content_hash": content_hash,
            "content": _json(content),
            "diff": _json({"set": content, "removed": []}),
        },
    )
    bind.execute(
        sa.text("UPDATE ai_asset_records SET active_revision_id=:revision_id WHERE id=:asset_id"),
        {"revision_id": revision_id, "asset_id": asset_id},
    )


def _backfill_version_bound_workflows() -> None:
    bind = op.get_bind()
    rows = list(
        bind.execute(
            sa.text(
                """
                SELECT id, tenant_id, name, definition_version, definition_json, status,
                       visibility_scope, call_policy, owner_type, owner_id,
                       created_by_user_id, created_by_agent_id
                FROM workflow_definitions
                WHERE tenant_id IS NOT NULL
                ORDER BY tenant_id, name, definition_version
                """
            )
        ).mappings()
    )
    # Preserve aggregate/history on the legacy latest-version record by moving
    # it to the exact key before seeding older immutable definitions.
    for row in rows:
        bind.execute(
            sa.text(
                """
                UPDATE ai_asset_records
                SET native_key=:versioned_key
                WHERE tenant_id=:tenant_id AND asset_type='workflow'
                  AND native_key=:legacy_key
                  AND (native_locator_json->>'version')::integer=:definition_version
                  AND NOT EXISTS (
                      SELECT 1 FROM ai_asset_records existing
                      WHERE existing.tenant_id=:tenant_id AND existing.asset_type='workflow'
                        AND existing.native_key=:versioned_key
                  )
                """
            ),
            {
                "tenant_id": row["tenant_id"],
                "legacy_key": f"workflow:{row['name']}",
                "versioned_key": f"workflow:{row['name']}@{row['definition_version']}",
                "definition_version": row["definition_version"],
            },
        )
    for row in rows:
        _seed_workflow_asset(bind, row)


def upgrade() -> None:
    create_table_if_missing(
        op,
        "ai_asset_usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_asset_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("config_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("native_key", sa.String(length=500), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("usage_kind", sa.String(length=80), nullable=False),
        sa.Column("usage_units", sa.Integer(), server_default="1", nullable=False),
        sa.Column("idempotency_key", sa.String(length=500), nullable=False),
        sa.Column("runtime_task_id", sa.String(length=100), nullable=True),
        sa.Column("session_id", sa.String(length=100), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("span_id", sa.String(length=128), nullable=True),
        sa.Column("tool_call_id", sa.String(length=200), nullable=True),
        sa.Column("evidence_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "asset_id", "idempotency_key", name="uq_ai_asset_usage_event_idempotency"),
    )
    create_index_if_missing(
        op, "ix_ai_asset_usage_events_asset_created", "ai_asset_usage_events", ["asset_id", "created_at"]
    )
    create_index_if_missing(
        op, "ix_ai_asset_usage_events_tenant_kind", "ai_asset_usage_events", ["tenant_id", "usage_kind"]
    )
    for column in (
        "tenant_id",
        "asset_id",
        "asset_revision_id",
        "usage_kind",
        "runtime_task_id",
        "session_id",
        "trace_id",
        "span_id",
        "tool_call_id",
    ):
        create_index_if_missing(op, f"ix_ai_asset_usage_events_{column}", "ai_asset_usage_events", [column])

    _backfill_version_bound_workflows()

    # Preserve each retained bounded evidence item as a version-bound event.
    op.execute(
        r"""
        INSERT INTO ai_asset_usage_events (
            id, tenant_id, asset_id, asset_revision_id, revision_version,
            content_hash, native_key, source_ref, usage_kind, usage_units,
            idempotency_key, runtime_task_id, session_id, trace_id, span_id,
            tool_call_id, evidence_json, created_at
        )
        SELECT
            gen_random_uuid(), asset.tenant_id, asset.id, revision.id, revision.version,
            asset.content_hash, asset.native_key, asset.source_ref,
            COALESCE(NULLIF(evidence.item->>'kind', ''), 'legacy_evidence'), 1,
            COALESCE(NULLIF(evidence.item->>'idempotency_key', ''),
                     'legacy-evidence:' || asset.id::text || ':' || evidence.ordinality::text),
            evidence.item->>'runtime_task_id', evidence.item->>'session_id',
            evidence.item->>'trace_id', evidence.item->>'span_id', evidence.item->>'tool_call_id',
            evidence.item, COALESCE(asset.last_used_at, asset.updated_at, now())
        FROM ai_asset_records AS asset
        JOIN config_revisions AS revision ON revision.id = asset.active_revision_id
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(asset.usage_evidence_json, '[]'::jsonb))
            WITH ORDINALITY AS evidence(item, ordinality)
        ON CONFLICT (tenant_id, asset_id, idempotency_key) DO NOTHING
        """
    )
    # Bounded JSON historically dropped older rows. One residual event keeps
    # the aggregate count mechanically reconcilable without fabricating detail.
    op.execute(
        r"""
        INSERT INTO ai_asset_usage_events (
            id, tenant_id, asset_id, asset_revision_id, revision_version,
            content_hash, native_key, source_ref, usage_kind, usage_units,
            idempotency_key, evidence_json, created_at
        )
        SELECT
            gen_random_uuid(), asset.tenant_id, asset.id, revision.id, revision.version,
            asset.content_hash, asset.native_key, asset.source_ref, 'legacy_residual',
            GREATEST(asset.usage_count - jsonb_array_length(COALESCE(asset.usage_evidence_json, '[]'::jsonb)), 0),
            'legacy-residual:' || asset.id::text,
            jsonb_build_object('kind', 'legacy_residual', 'reason', 'bounded_usage_evidence_history'),
            COALESCE(asset.last_used_at, asset.updated_at, now())
        FROM ai_asset_records AS asset
        JOIN config_revisions AS revision ON revision.id = asset.active_revision_id
        WHERE asset.usage_count > jsonb_array_length(COALESCE(asset.usage_evidence_json, '[]'::jsonb))
        ON CONFLICT (tenant_id, asset_id, idempotency_key) DO NOTHING
        """
    )

    # Old pending approvals did not bind the resolved revision. They remain
    # auditable but must be submitted again before execution.
    op.execute(
        r"""
        UPDATE approval_requests
        SET status = 'rejected',
            execution_status = 'needs_reapproval',
            resolved_at = COALESCE(resolved_at, now()),
            details = COALESCE(details::jsonb, '{}'::jsonb) || jsonb_build_object(
                'asset_revision_reapproval_reason', 'approval envelope predates resolved asset refs',
                'asset_revision_previous_execution_status', execution_status,
                'asset_revision_previous_status', status::text,
                'asset_revision_previous_resolved_at', resolved_at
            )
        WHERE execution_status IN ('pending', 'approved')
          AND (
              tool_name IN ('load_skill', 'run_skill_tool', 'spawn_subagent', 'call_mcp_tool')
              OR left(tool_name, 5) = 'mcp__'
          )
          AND COALESCE(execution_envelope->>'schema', '') <> 'hive.approval_execution_envelope.v2'
        """
    )

    op.execute("ALTER TABLE ai_asset_usage_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ai_asset_usage_events FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_ai_asset_usage_events ON ai_asset_usage_events")
    op.execute(
        """
        CREATE POLICY tenant_isolation_ai_asset_usage_events ON ai_asset_usage_events
        USING (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        WITH CHECK (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        UPDATE approval_requests
        SET status = COALESCE(details->>'asset_revision_previous_status', 'pending')::approval_status_enum,
            execution_status = COALESCE(details->>'asset_revision_previous_execution_status', 'pending'),
            resolved_at = CASE
                WHEN (details::jsonb)->'asset_revision_previous_resolved_at' IS NULL
                  OR (details::jsonb)->'asset_revision_previous_resolved_at' = 'null'::jsonb
                THEN NULL
                ELSE ((details::jsonb)->>'asset_revision_previous_resolved_at')::timestamptz
            END,
            details = COALESCE(details::jsonb, '{}'::jsonb) - 'asset_revision_reapproval_reason'
                                                           - 'asset_revision_previous_execution_status'
                                                           - 'asset_revision_previous_status'
                                                           - 'asset_revision_previous_resolved_at'
        WHERE execution_status = 'needs_reapproval'
          AND (details::jsonb) ? 'asset_revision_reapproval_reason'
        """
    )
    op.execute(
        r"""
        UPDATE ai_asset_records AS asset
        SET native_key = 'workflow:' || asset.display_name
        WHERE asset.asset_type = 'workflow'
          AND asset.native_key LIKE 'workflow:%@%'
          AND (asset.native_locator_json->>'version')::integer = (
              SELECT max((candidate.native_locator_json->>'version')::integer)
              FROM ai_asset_records AS candidate
              WHERE candidate.tenant_id = asset.tenant_id
                AND candidate.asset_type = 'workflow'
                AND candidate.display_name = asset.display_name
          )
          AND NOT EXISTS (
              SELECT 1 FROM ai_asset_records AS legacy
              WHERE legacy.tenant_id = asset.tenant_id
                AND legacy.asset_type = 'workflow'
                AND legacy.native_key = 'workflow:' || asset.display_name
          )
        """
    )
    op.drop_table("ai_asset_usage_events")
