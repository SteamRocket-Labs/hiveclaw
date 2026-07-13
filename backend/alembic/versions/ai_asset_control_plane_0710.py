"""Add the thin enterprise AI asset control plane and backfill DB assets.

Revision ID: ai_asset_control_plane_0710
Revises: runtime_assembly_nested_0710
Create Date: 2026-07-10
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.migration_compat import add_column_if_missing, create_index_if_missing, create_table_if_missing


revision = "ai_asset_control_plane_0710"
down_revision = "runtime_assembly_nested_0710"
branch_labels = None
depends_on = None

_AI_ASSET_TABLES = ("ai_asset_records",)


def _canonical_hash(content: dict[str, Any]) -> str:
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _seed_asset(
    bind,
    *,
    tenant_id,
    asset_type: str,
    native_entity_id,
    native_key: str,
    locator: dict[str, Any],
    display_name: str,
    owner_type: str,
    owner_id,
    visibility_scope: str,
    lifecycle_status: str,
    content: dict[str, Any],
    source_type: str,
    source_ref: str | None,
    trust_state: str,
    dependencies: list[Any] | None = None,
    compatibility: dict[str, Any] | None = None,
    admission_state: str = "admitted",
    quarantine_reason: str | None = None,
    created_by_user_id=None,
    created_by_agent_id=None,
) -> None:
    asset_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    content_hash = _canonical_hash(content)
    bind.execute(
        sa.text(
            """
            INSERT INTO ai_asset_records (
                id, tenant_id, asset_type, native_entity_id, native_key, native_locator_json,
                display_name, owner_type, owner_id, visibility_scope, lifecycle_status,
                content_hash, source_type, source_ref, trust_state, dependencies_json,
                compatibility_json, admission_state, quarantine_reason, usage_count,
                usage_evidence_json, projection_status, created_by_user_id, created_by_agent_id,
                created_at, updated_at
            ) VALUES (
                :id, :tenant_id, :asset_type, :native_entity_id, :native_key,
                CAST(:locator AS jsonb), :display_name, :owner_type, :owner_id,
                :visibility_scope, :lifecycle_status, :content_hash, :source_type,
                :source_ref, :trust_state, CAST(:dependencies AS jsonb),
                CAST(:compatibility AS jsonb), :admission_state, :quarantine_reason, 0,
                '[]'::jsonb, 'applied', :created_by_user_id, :created_by_agent_id, now(), now()
            )
            ON CONFLICT (tenant_id, asset_type, native_key) DO NOTHING
            """
        ),
        {
            "id": asset_id,
            "tenant_id": tenant_id,
            "asset_type": asset_type,
            "native_entity_id": native_entity_id,
            "native_key": native_key,
            "locator": _json(locator),
            "display_name": display_name,
            "owner_type": owner_type,
            "owner_id": owner_id,
            "visibility_scope": visibility_scope,
            "lifecycle_status": lifecycle_status,
            "content_hash": content_hash,
            "source_type": source_type,
            "source_ref": source_ref,
            "trust_state": trust_state,
            "dependencies": _json(dependencies or []),
            "compatibility": _json(compatibility or {}),
            "admission_state": admission_state,
            "quarantine_reason": quarantine_reason,
            "created_by_user_id": created_by_user_id,
            "created_by_agent_id": created_by_agent_id,
        },
    )
    inserted = bind.execute(
        sa.text(
            "SELECT id FROM ai_asset_records "
            "WHERE tenant_id=:tenant_id AND asset_type=:asset_type AND native_key=:native_key"
        ),
        {"tenant_id": tenant_id, "asset_type": asset_type, "native_key": native_key},
    ).scalar_one()
    if inserted != asset_id:
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
                'AI asset control-plane backfill', true, now()
            )
            """
        ),
        {
            "id": revision_id,
            "asset_id": asset_id,
            "tenant_id": tenant_id,
            "content_hash": content_hash,
            "content": _json(content),
            "diff": _json({"set": content, "removed": []}),
        },
    )
    bind.execute(
        sa.text("UPDATE ai_asset_records SET active_revision_id=:revision_id WHERE id=:asset_id"),
        {"revision_id": revision_id, "asset_id": asset_id},
    )


def _backfill_agents(bind) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT id, tenant_id, name, role_description, bio, avatar_url, creator_id,
                   sponsor_user_id, owner_user_id, primary_model_id, fallback_model_id,
                   agent_type, agent_class, security_zone, execution_mode, smart_model_routing,
                   context_window_size, max_tool_rounds, max_triggers, min_poll_interval_min,
                   webhook_rate_limit, timezone, subagent_evolution_auto_approve,
                   deleted_at, deactivated_at
            FROM agents WHERE tenant_id IS NOT NULL
            """
        )
    ).mappings()
    for row in rows:
        owner_id = row["owner_user_id"] or row["sponsor_user_id"] or row["creator_id"]
        config = {
            key: row[key]
            for key in (
                "name",
                "role_description",
                "bio",
                "avatar_url",
                "primary_model_id",
                "fallback_model_id",
                "agent_type",
                "agent_class",
                "security_zone",
                "execution_mode",
                "smart_model_routing",
                "context_window_size",
                "max_tool_rounds",
                "max_triggers",
                "min_poll_interval_min",
                "webhook_rate_limit",
                "timezone",
                "subagent_evolution_auto_approve",
            )
        }
        lifecycle = "deleted" if row["deleted_at"] else "deprecated" if row["deactivated_at"] else "active"
        _seed_asset(
            bind,
            tenant_id=row["tenant_id"],
            asset_type="agent",
            native_entity_id=row["id"],
            native_key=f"agent:{row['id']}",
            locator={"agent_id": str(row["id"])},
            display_name=row["name"],
            owner_type="user",
            owner_id=owner_id,
            visibility_scope="tenant",
            lifecycle_status=lifecycle,
            content={
                "schema": "hive.ai_asset.agent.v1",
                "asset_type": "agent",
                "config": config,
                "control": {"lifecycle_status": lifecycle},
            },
            source_type="native",
            source_ref=f"agents/{row['id']}",
            trust_state="trusted",
            dependencies=[str(value) for value in (row["primary_model_id"], row["fallback_model_id"]) if value],
            created_by_user_id=row["creator_id"],
        )


def _backfill_skills(bind) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT id, tenant_id, name, description, category, icon, folder_name,
                   is_builtin, is_default
            FROM skills WHERE tenant_id IS NOT NULL
            """
        )
    ).mappings()
    for row in rows:
        files = [
            {"path": item["path"], "content": item["content"]}
            for item in bind.execute(
                sa.text("SELECT path, content FROM skill_files WHERE skill_id=:skill_id ORDER BY path"),
                {"skill_id": row["id"]},
            ).mappings()
        ]
        content = {
            "schema": "hive.ai_asset.skill.v1",
            "asset_type": "skill",
            "config": {key: row[key] for key in ("name", "description", "category", "icon", "folder_name")},
            "files": files,
            "control": {
                "lifecycle_status": "quarantined",
                "trust_state": "review_required",
                "admission_state": "review_required",
            },
        }
        _seed_asset(
            bind,
            tenant_id=row["tenant_id"],
            asset_type="skill",
            native_entity_id=row["id"],
            native_key=f"skill:{row['id']}",
            locator={"kind": "registry", "skill_id": str(row["id"])},
            display_name=row["name"],
            owner_type="tenant",
            owner_id=None,
            visibility_scope="tenant",
            lifecycle_status="quarantined",
            content=content,
            source_type="registry",
            source_ref=f"skills/{row['folder_name']}",
            trust_state="review_required",
            admission_state="review_required",
            quarantine_reason="legacy skill owner could not be inferred",
        )


def _backfill_workflows(bind) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT DISTINCT ON (tenant_id, name)
                   id, tenant_id, name, definition_version, definition_hash,
                   definition_json, status, visibility_scope, call_policy,
                   owner_type, owner_id, created_by_user_id, created_by_agent_id
            FROM workflow_definitions
            WHERE tenant_id IS NOT NULL
            ORDER BY tenant_id, name, definition_version DESC
            """
        )
    ).mappings()
    for row in rows:
        content = {
            "schema": "hive.ai_asset.workflow.v1",
            "asset_type": "workflow",
            "definition": row["definition_json"],
            "control": {
                "status": row["status"],
                "visibility_scope": row["visibility_scope"],
                "call_policy": row["call_policy"],
                "owner_type": row["owner_type"],
                "owner_id": row["owner_id"],
            },
        }
        _seed_asset(
            bind,
            tenant_id=row["tenant_id"],
            asset_type="workflow",
            native_entity_id=row["id"],
            native_key=f"workflow:{row['name']}",
            locator={"name": row["name"], "version": row["definition_version"]},
            display_name=row["name"],
            owner_type=row["owner_type"],
            owner_id=row["owner_id"],
            visibility_scope=row["visibility_scope"],
            lifecycle_status=row["status"],
            content=content,
            source_type="workflow_registry",
            source_ref=f"workflow:{row['name']}@{row['definition_version']}",
            trust_state="trusted",
            dependencies=list((row["definition_json"] or {}).get("dependencies") or []),
            created_by_user_id=row["created_by_user_id"],
            created_by_agent_id=row["created_by_agent_id"],
        )


def _backfill_external_capabilities(bind) -> None:
    rows = bind.execute(sa.text("SELECT * FROM external_capability_snapshots")).mappings()
    for row in rows:
        content = {
            "schema": "hive.ai_asset.external_capability.v1",
            "asset_type": "external_capability",
            "snapshot_id": str(row["id"]),
            "snapshot_key": row["snapshot_key"],
            "source_hash": row["source_hash"],
            "component_manifest": row["component_manifest_json"],
            "governance_projection": row["governance_projection_json"],
            "control": {
                "lifecycle_status": "revoked" if row["status"] == "revoked" else "active",
                "trust_state": "revoked" if row["status"] == "revoked" else "trusted",
                "admission_state": "revoked" if row["status"] == "revoked" else "admitted",
            },
        }
        status = "revoked" if row["status"] == "revoked" else "active"
        _seed_asset(
            bind,
            tenant_id=row["tenant_id"],
            asset_type="external_capability",
            native_entity_id=row["id"],
            native_key=f"external:{row['snapshot_key']}",
            locator={"snapshot_key": row["snapshot_key"], "snapshot_id": str(row["id"])},
            display_name=row["normalized_name"],
            owner_type="tenant",
            owner_id=row["approved_by_user_id"],
            visibility_scope="tenant",
            lifecycle_status=status,
            content=content,
            source_type=row["source_format"],
            source_ref=row["source_uri"],
            trust_state="revoked" if status == "revoked" else "trusted",
            dependencies=list((row["component_manifest_json"] or {}).get("dependencies") or []),
            compatibility={"admission_class": row["admission_class"], "source_ref": row["source_ref"]},
            admission_state="revoked" if status == "revoked" else "admitted",
            created_by_user_id=row["approved_by_user_id"],
        )


def upgrade() -> None:
    add_column_if_missing(
        op,
        "config_revisions",
        sa.Column(
            "parent_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("config_revisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    add_column_if_missing(
        op,
        "config_revisions",
        sa.Column(
            "rollback_of_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("config_revisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    create_index_if_missing(op, "ix_config_revisions_parent_revision_id", "config_revisions", ["parent_revision_id"])
    create_index_if_missing(
        op, "ix_config_revisions_rollback_of_revision_id", "config_revisions", ["rollback_of_revision_id"]
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_config_revision_immutability()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'config revision snapshots are immutable';
            END IF;
            IF ROW(
                OLD.id, OLD.entity_type, OLD.entity_id, OLD.tenant_id, OLD.version,
                OLD.content_hash, OLD.content, OLD.diff_from_prev, OLD.change_source,
                OLD.changed_by_user_id, OLD.changed_by_agent_id, OLD.change_message,
                OLD.parent_revision_id, OLD.rollback_of_revision_id, OLD.created_at
            ) IS DISTINCT FROM ROW(
                NEW.id, NEW.entity_type, NEW.entity_id, NEW.tenant_id, NEW.version,
                NEW.content_hash, NEW.content, NEW.diff_from_prev, NEW.change_source,
                NEW.changed_by_user_id, NEW.changed_by_agent_id, NEW.change_message,
                NEW.parent_revision_id, NEW.rollback_of_revision_id, NEW.created_at
            ) OR (OLD.is_active = false AND NEW.is_active = true) THEN
                RAISE EXCEPTION 'config revision snapshots are immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_config_revision_immutability
        BEFORE UPDATE OR DELETE ON config_revisions
        FOR EACH ROW EXECUTE FUNCTION enforce_config_revision_immutability()
        """
    )

    create_table_if_missing(
        op,
        "ai_asset_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("asset_type", sa.String(length=40), nullable=False),
        sa.Column("native_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("native_key", sa.String(length=500), nullable=False),
        sa.Column("native_locator_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=False),
        sa.Column("owner_type", sa.String(length=30), server_default="tenant", nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("visibility_scope", sa.String(length=30), server_default="tenant", nullable=False),
        sa.Column("lifecycle_status", sa.String(length=30), server_default="draft", nullable=False),
        sa.Column(
            "active_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("config_revisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=50), server_default="native", nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("trust_state", sa.String(length=30), server_default="trusted", nullable=False),
        sa.Column("dependencies_json", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("compatibility_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("admission_state", sa.String(length=30), server_default="admitted", nullable=False),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.Column("usage_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usage_evidence_json", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("projection_status", sa.String(length=30), server_default="applied", nullable=False),
        sa.Column("projection_error", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "published_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "revoked_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "asset_type", "native_key", name="uq_ai_asset_native_key"),
        sa.CheckConstraint(
            "asset_type IN ('agent','skill','workflow','subagent','external_capability')",
            name="ck_ai_asset_type",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('draft','active','deprecated','revoked','deleted','quarantined')",
            name="ck_ai_asset_lifecycle_status",
        ),
        sa.CheckConstraint(
            "projection_status IN ('pending','applied','failed','drifted')",
            name="ck_ai_asset_projection_status",
        ),
    )
    for name, columns in (
        ("ix_ai_asset_records_tenant_id", ["tenant_id"]),
        ("ix_ai_asset_records_asset_type", ["asset_type"]),
        ("ix_ai_asset_records_native_entity_id", ["native_entity_id"]),
        ("ix_ai_asset_records_active_revision_id", ["active_revision_id"]),
        ("ix_ai_asset_records_trust_state", ["trust_state"]),
        ("ix_ai_asset_records_admission_state", ["admission_state"]),
        ("ix_ai_asset_tenant_type_status", ["tenant_id", "asset_type", "lifecycle_status"]),
        ("ix_ai_asset_content_hash", ["content_hash"]),
    ):
        create_index_if_missing(op, name, "ai_asset_records", columns)

    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE ai_asset_records ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE ai_asset_records FORCE ROW LEVEL SECURITY")
        op.execute("DROP POLICY IF EXISTS tenant_isolation_ai_asset_records ON ai_asset_records")
        op.execute(
            """
            CREATE POLICY tenant_isolation_ai_asset_records ON ai_asset_records
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
        bind = op.get_bind()
        _backfill_agents(bind)
        _backfill_skills(bind)
        _backfill_workflows(bind)
        _backfill_external_capabilities(bind)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_config_revision_immutability ON config_revisions")
    op.execute("DROP FUNCTION IF EXISTS enforce_config_revision_immutability()")
    op.execute("DELETE FROM config_revisions WHERE entity_type = 'ai_asset'")
    op.drop_table("ai_asset_records")
    op.drop_index("ix_config_revisions_rollback_of_revision_id", table_name="config_revisions")
    op.drop_index("ix_config_revisions_parent_revision_id", table_name="config_revisions")
    op.drop_column("config_revisions", "rollback_of_revision_id")
    op.drop_column("config_revisions", "parent_revision_id")
