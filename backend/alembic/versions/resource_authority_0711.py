"""Add user-owned Agent resource authority manifests.

Revision ID: resource_authority_0711
Revises: ai_asset_usage_events_0711
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
)


revision = "resource_authority_0711"
down_revision = "ai_asset_usage_events_0711"
branch_labels = None
depends_on = None


def _tenant_rls(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(f'DROP POLICY IF EXISTS "tenant_isolation_{table}" ON "{table}"')
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table} ON {table}
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


def upgrade() -> None:
    create_table_if_missing(
        op,
        "workspace_resource_manifests",
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
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "root_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("authority_state", sa.String(length=24), server_default="quarantined", nullable=False),
        sa.Column("source", sa.String(length=64), server_default="legacy_backfill", nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "authority_state IN ('owned','quarantined')",
            name="ck_workspace_resource_manifest_authority_state",
        ),
        sa.UniqueConstraint("agent_id", "path", name="uq_workspace_resource_manifest_agent_path"),
    )
    for column in ("tenant_id", "agent_id", "owner_user_id", "root_session_id", "authority_state"):
        create_index_if_missing(
            op, f"ix_workspace_resource_manifests_{column}", "workspace_resource_manifests", [column]
        )
    create_index_if_missing(
        op,
        "ix_workspace_resource_manifests_agent_path_prefix",
        "workspace_resource_manifests",
        ["agent_id", "path"],
    )

    add_column_if_missing(
        op, "chat_artifacts", sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    add_column_if_missing(
        op, "chat_artifacts", sa.Column("root_session_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    add_column_if_missing(
        op,
        "chat_artifacts",
        sa.Column("authority_state", sa.String(length=24), server_default="quarantined", nullable=False),
    )
    create_foreign_key_if_missing(
        op,
        "fk_chat_artifacts_owner_user_id_users",
        "chat_artifacts",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    create_foreign_key_if_missing(
        op,
        "fk_chat_artifacts_root_session_id_chat_sessions",
        "chat_artifacts",
        "chat_sessions",
        ["root_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    for column in ("owner_user_id", "root_session_id", "authority_state"):
        create_index_if_missing(op, f"ix_chat_artifacts_{column}", "chat_artifacts", [column])

    add_column_if_missing(op, "tasks", sa.Column("root_session_id", postgresql.UUID(as_uuid=True), nullable=True))
    add_column_if_missing(
        op, "tasks", sa.Column("authority_state", sa.String(length=24), server_default="owned", nullable=False)
    )
    create_foreign_key_if_missing(
        op,
        "fk_tasks_root_session_id_chat_sessions",
        "tasks",
        "chat_sessions",
        ["root_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    create_index_if_missing(op, "ix_tasks_root_session_id", "tasks", ["root_session_id"])
    create_index_if_missing(op, "ix_tasks_authority_state", "tasks", ["authority_state"])

    add_column_if_missing(
        op, "agent_activity_logs", sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    add_column_if_missing(
        op, "agent_activity_logs", sa.Column("root_session_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    add_column_if_missing(
        op,
        "agent_activity_logs",
        sa.Column("authority_state", sa.String(length=24), server_default="quarantined", nullable=False),
    )
    create_foreign_key_if_missing(
        op,
        "fk_agent_activity_logs_owner_user_id_users",
        "agent_activity_logs",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    create_foreign_key_if_missing(
        op,
        "fk_agent_activity_logs_root_session_id_chat_sessions",
        "agent_activity_logs",
        "chat_sessions",
        ["root_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    for column in ("owner_user_id", "root_session_id", "authority_state"):
        create_index_if_missing(op, f"ix_agent_activity_logs_{column}", "agent_activity_logs", [column])

    # Chat session ownership is the only trustworthy legacy artifact authority.
    op.execute(
        """
        UPDATE chat_artifacts AS artifact
        SET owner_user_id = session.user_id,
            root_session_id = COALESCE(session.root_session_id, session.id),
            authority_state = CASE WHEN session.user_id IS NULL THEN 'quarantined' ELSE 'owned' END
        FROM chat_sessions AS session
        WHERE session.id = artifact.session_id
        """
    )
    # Tasks already have a non-null creator.  Recover a confirmed-plan session
    # only when it belongs to the same creator; otherwise keep the owner without
    # fabricating a session relationship.
    op.execute(
        """
        UPDATE tasks AS task
        SET root_session_id = COALESCE(session.root_session_id, session.id),
            authority_state = 'owned'
        FROM chat_sessions AS session
        WHERE session.id = NULLIF(task.plan_authorization->>'session_id', '')::uuid
          AND session.agent_id = task.agent_id
          AND session.user_id = task.created_by
        """
    )
    # Activity detail historically carried session ids without normalized
    # columns.  Only a tenant/Agent/session match is admitted; every other row
    # remains admin-only quarantine.
    op.execute(
        r"""
        WITH normalized_activity AS MATERIALIZED (
            SELECT activity.id,
                   activity.agent_id,
                   activity.tenant_id,
                   replace(activity.detail_json::text, '\u0000', '\uFFFD')::jsonb AS detail_json
            FROM agent_activity_logs AS activity
            WHERE activity.detail_json IS NOT NULL
        ),
        matched_activity AS (
            SELECT normalized.id,
                   session.user_id AS owner_user_id,
                   COALESCE(session.root_session_id, session.id) AS root_session_id
            FROM normalized_activity AS normalized
            JOIN chat_sessions AS session
              ON session.id = CASE
                  WHEN COALESCE(normalized.detail_json->>'root_session_id', normalized.detail_json->>'session_id', '')
                       ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                  THEN COALESCE(
                      normalized.detail_json->>'root_session_id',
                      normalized.detail_json->>'session_id'
                  )::uuid
                  ELSE NULL
              END
             AND session.agent_id = normalized.agent_id
             AND session.tenant_id = normalized.tenant_id
        )
        UPDATE agent_activity_logs AS activity
        SET owner_user_id = matched.owner_user_id,
            root_session_id = matched.root_session_id,
            authority_state = CASE WHEN matched.owner_user_id IS NULL THEN 'quarantined' ELSE 'owned' END
        FROM matched_activity AS matched
        WHERE activity.id = matched.id
        """
    )
    # Delivered artifacts are sufficient evidence to seed the mutable path
    # manifest. Remaining on-disk legacy files have no SQL-verifiable owner and
    # are intentionally absent: runtime treats a missing manifest as quarantine.
    op.execute(
        """
        WITH artifact_authority AS (
            SELECT artifact.tenant_id,
                   artifact.agent_id,
                   artifact.path,
                   CASE
                       WHEN BOOL_AND(artifact.owner_user_id IS NOT NULL)
                        AND COUNT(DISTINCT artifact.owner_user_id) = 1
                       THEN MIN(artifact.owner_user_id::text)::uuid
                       ELSE NULL
                   END AS owner_user_id,
                   CASE
                       WHEN BOOL_AND(artifact.root_session_id IS NOT NULL)
                        AND COUNT(DISTINCT artifact.root_session_id) = 1
                       THEN MIN(artifact.root_session_id::text)::uuid
                       ELSE NULL
                   END AS root_session_id,
                   CASE
                       WHEN BOOL_AND(artifact.owner_user_id IS NOT NULL)
                        AND COUNT(DISTINCT artifact.owner_user_id) = 1
                        AND BOOL_AND(artifact.authority_state = 'owned')
                       THEN 'owned'
                       ELSE 'quarantined'
                   END AS authority_state,
                   CASE
                       WHEN BOOL_AND(NULLIF(artifact.snapshot_json->>'content_hash', '') IS NOT NULL)
                        AND COUNT(DISTINCT artifact.snapshot_json->>'content_hash') = 1
                       THEN MIN(artifact.snapshot_json->>'content_hash')
                       ELSE NULL
                   END AS content_hash,
                   MIN(artifact.created_at) AS created_at,
                   MAX(artifact.created_at) AS updated_at
            FROM chat_artifacts AS artifact
            WHERE artifact.tenant_id IS NOT NULL
            GROUP BY artifact.tenant_id, artifact.agent_id, artifact.path
        )
        INSERT INTO workspace_resource_manifests (
            id, tenant_id, agent_id, path, owner_user_id, root_session_id,
            authority_state, source, content_hash, created_at, updated_at
        )
        SELECT gen_random_uuid(), authority.tenant_id, authority.agent_id, authority.path,
               authority.owner_user_id, authority.root_session_id, authority.authority_state,
               'chat_artifact_backfill', authority.content_hash,
               authority.created_at, authority.updated_at
        FROM artifact_authority AS authority
        """
    )

    _tenant_rls("workspace_resource_manifests")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_workspace_resource_manifests ON workspace_resource_manifests")
    for column in ("authority_state", "root_session_id", "owner_user_id"):
        op.drop_index(f"ix_agent_activity_logs_{column}", table_name="agent_activity_logs")
    # DROP COLUMN removes its local FK regardless of whether this revision
    # created the explicit Alembic name or fresh-bootstrap create_all used the
    # PostgreSQL default name.  Naming either constraint here makes downgrade
    # fail on one of those two supported schema construction paths.
    for column in ("authority_state", "root_session_id", "owner_user_id"):
        op.drop_column("agent_activity_logs", column)
    op.drop_index("ix_tasks_authority_state", table_name="tasks")
    op.drop_index("ix_tasks_root_session_id", table_name="tasks")
    op.drop_column("tasks", "authority_state")
    op.drop_column("tasks", "root_session_id")
    for column in ("authority_state", "root_session_id", "owner_user_id"):
        op.drop_index(f"ix_chat_artifacts_{column}", table_name="chat_artifacts")
    for column in ("authority_state", "root_session_id", "owner_user_id"):
        op.drop_column("chat_artifacts", column)
    op.drop_table("workspace_resource_manifests")
