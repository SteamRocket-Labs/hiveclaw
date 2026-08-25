"""Add the provider-neutral Agent Environment control-plane authority.

Revision ID: agent_environment_cp_0825
Revises: merge_incident_kimi_0725
Create Date: 2026-08-25 09:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "agent_environment_cp_0825"
down_revision = "merge_incident_kimi_0725"
branch_labels = None
depends_on = None

_TABLES = (
    "execution_environments",
    "environment_sessions",
    "environment_leases",
    "environment_checkpoints",
)


def _enable_strict_tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table} ON {table}
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


def _install_tenant_binding_triggers() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.enforce_environment_tenant_binding()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF TG_TABLE_NAME = 'execution_environments' THEN
            IF NOT EXISTS (
              SELECT 1 FROM agents a WHERE a.id = NEW.agent_id AND a.tenant_id = NEW.tenant_id
            ) THEN
              RAISE EXCEPTION 'execution environment Agent tenant mismatch' USING ERRCODE = '23514';
            END IF;
            IF NEW.owner_runtime_task_id IS NOT NULL AND NOT EXISTS (
              SELECT 1 FROM runtime_tasks r
              WHERE r.id = NEW.owner_runtime_task_id AND r.tenant_id = NEW.tenant_id
            ) THEN
              RAISE EXCEPTION 'execution environment RuntimeTask tenant mismatch' USING ERRCODE = '23514';
            END IF;
          ELSIF TG_TABLE_NAME = 'environment_leases' THEN
            IF NOT EXISTS (
              SELECT 1 FROM agents a WHERE a.id = NEW.agent_id AND a.tenant_id = NEW.tenant_id
            ) OR NOT EXISTS (
              SELECT 1 FROM runtime_tasks r
              WHERE r.id = NEW.runtime_task_id AND r.tenant_id = NEW.tenant_id
            ) THEN
              RAISE EXCEPTION 'environment lease authority tenant mismatch' USING ERRCODE = '23514';
            END IF;
          ELSIF TG_TABLE_NAME = 'environment_checkpoints' AND NEW.source_runtime_task_id IS NOT NULL THEN
            IF NOT EXISTS (
              SELECT 1 FROM runtime_tasks r
              WHERE r.id = NEW.source_runtime_task_id AND r.tenant_id = NEW.tenant_id
            ) THEN
              RAISE EXCEPTION 'environment checkpoint RuntimeTask tenant mismatch' USING ERRCODE = '23514';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE EXECUTE ON FUNCTION public.enforce_environment_tenant_binding() FROM PUBLIC")
    for table in ("execution_environments", "environment_leases", "environment_checkpoints"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_tenant_binding
            BEFORE INSERT OR UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION public.enforce_environment_tenant_binding()
            """
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.enforce_runtime_task_environment_binding()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF NEW.environment_id IS NULL AND (
            NEW.environment_session_id IS NOT NULL
            OR NEW.environment_lease_id IS NOT NULL
            OR NEW.environment_checkpoint_id IS NOT NULL
          ) THEN
            RAISE EXCEPTION 'RuntimeTask environment child ref requires environment_id' USING ERRCODE = '23514';
          END IF;
          IF NEW.environment_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM execution_environments e
            WHERE e.id = NEW.environment_id AND e.tenant_id = NEW.tenant_id
          ) THEN
            RAISE EXCEPTION 'RuntimeTask environment tenant mismatch' USING ERRCODE = '23514';
          END IF;
          IF NEW.environment_session_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM environment_sessions s
            WHERE s.id = NEW.environment_session_id
              AND s.environment_id = NEW.environment_id
              AND s.tenant_id = NEW.tenant_id
          ) THEN
            RAISE EXCEPTION 'RuntimeTask environment session mismatch' USING ERRCODE = '23514';
          END IF;
          IF NEW.environment_lease_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM environment_leases l
            WHERE l.id = NEW.environment_lease_id
              AND l.environment_id = NEW.environment_id
              AND l.runtime_task_id = NEW.id
              AND l.tenant_id = NEW.tenant_id
          ) THEN
            RAISE EXCEPTION 'RuntimeTask environment lease mismatch' USING ERRCODE = '23514';
          END IF;
          IF NEW.environment_checkpoint_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM environment_checkpoints c
            WHERE c.id = NEW.environment_checkpoint_id
              AND c.environment_id = NEW.environment_id
              AND c.tenant_id = NEW.tenant_id
          ) THEN
            RAISE EXCEPTION 'RuntimeTask environment checkpoint mismatch' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE EXECUTE ON FUNCTION public.enforce_runtime_task_environment_binding() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER trg_runtime_tasks_environment_binding
        BEFORE INSERT OR UPDATE OF
          tenant_id, environment_id, environment_session_id,
          environment_lease_id, environment_checkpoint_id
        ON runtime_tasks
        FOR EACH ROW EXECUTE FUNCTION public.enforce_runtime_task_environment_binding()
        """
    )


def upgrade() -> None:
    op.create_table(
        "execution_environments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_type", sa.String(length=24), nullable=False),
        sa.Column("parent_environment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_checkpoint_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_runtime_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("desired_state", sa.String(length=20), nullable=False),
        sa.Column("observed_state", sa.String(length=20), nullable=False),
        sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "capability_profile",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("policy_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("workspace_manifest_hash", sa.String(length=64), nullable=True),
        sa.Column("current_checkpoint_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope_type IN ('agent_private', 'task_fork')",
            name="ck_execution_environments_scope_type",
        ),
        sa.CheckConstraint(
            "desired_state IN ('running', 'stopped', 'destroyed')",
            name="ck_execution_environments_desired_state",
        ),
        sa.CheckConstraint(
            "observed_state IN ('pending', 'starting', 'ready', 'recovering', 'stopping', "
            "'stopped', 'unavailable', 'failed', 'destroyed')",
            name="ck_execution_environments_observed_state",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_execution_environments_generation_positive"),
        sa.CheckConstraint("row_version >= 1", name="ck_execution_environments_row_version_positive"),
        sa.CheckConstraint("char_length(policy_snapshot_hash) = 64", name="ck_execution_environments_policy_hash"),
        sa.CheckConstraint(
            "workspace_manifest_hash IS NULL OR char_length(workspace_manifest_hash) = 64",
            name="ck_execution_environments_workspace_hash",
        ),
        sa.CheckConstraint(
            "(scope_type = 'agent_private' AND parent_environment_id IS NULL "
            "AND source_checkpoint_id IS NULL AND owner_runtime_task_id IS NULL) OR "
            "(scope_type = 'task_fork' AND parent_environment_id IS NOT NULL "
            "AND source_checkpoint_id IS NOT NULL AND owner_runtime_task_id IS NOT NULL)",
            name="ck_execution_environments_scope_binding",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_runtime_task_id"], ["runtime_tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_environment_id"],
            ["execution_environments.tenant_id", "execution_environments.id"],
            name="fk_execution_environments_tenant_parent",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_execution_environments_tenant_id_id"),
    )
    op.create_index("ix_execution_environments_tenant_id", "execution_environments", ["tenant_id"])
    op.create_index("ix_execution_environments_agent_id", "execution_environments", ["agent_id"])
    op.create_index(
        "ix_execution_environments_owner_runtime_task_id",
        "execution_environments",
        ["owner_runtime_task_id"],
    )
    op.create_index("ix_execution_environments_observed_state", "execution_environments", ["observed_state"])
    op.create_index("ix_execution_environments_idle_expires_at", "execution_environments", ["idle_expires_at"])
    op.create_index(
        "ix_execution_environments_tenant_state",
        "execution_environments",
        ["tenant_id", "observed_state", "last_used_at"],
    )
    op.create_index(
        "uq_execution_environments_agent_private",
        "execution_environments",
        ["tenant_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'agent_private' AND deleted_at IS NULL"),
    )

    op.create_table(
        "environment_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("provider_resource_ref", sa.Text(), nullable=True),
        sa.Column("provider_session_ref", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column(
            "capability_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_receipt_ref", sa.Text(), nullable=True),
        sa.Column("redacted_error", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_environment_sessions_generation_positive"),
        sa.CheckConstraint(
            "state IN ('starting', 'ready', 'recovering', 'stopping', 'stopped', "
            "'unavailable', 'failed', 'destroyed')",
            name="ck_environment_sessions_state",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment_id"],
            ["execution_environments.tenant_id", "execution_environments.id"],
            name="fk_environment_sessions_tenant_environment",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_environment_sessions_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "environment_id",
            "id",
            name="uq_environment_sessions_tenant_environment_id",
        ),
        sa.UniqueConstraint("environment_id", "generation", name="uq_environment_sessions_generation"),
    )
    op.create_index("ix_environment_sessions_tenant_id", "environment_sessions", ["tenant_id"])
    op.create_index("ix_environment_sessions_environment_id", "environment_sessions", ["environment_id"])
    op.create_index("ix_environment_sessions_state", "environment_sessions", ["state"])
    op.create_index(
        "ix_environment_sessions_tenant_state",
        "environment_sessions",
        ["tenant_id", "state", "last_observed_at"],
    )
    op.create_index(
        "uq_environment_sessions_current_writable",
        "environment_sessions",
        ["environment_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('starting', 'ready', 'recovering')"),
    )

    op.create_table(
        "environment_leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("runtime_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("access_mode", sa.String(length=20), nullable=False),
        sa.Column("fence_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("access_mode IN ('read_only', 'read_write')", name="ck_environment_leases_access_mode"),
        sa.CheckConstraint(
            "status IN ('active', 'released', 'expired', 'revoked')",
            name="ck_environment_leases_status",
        ),
        sa.CheckConstraint("fence_version >= 1", name="ck_environment_leases_fence_positive"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["runtime_task_id"], ["runtime_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment_id"],
            ["execution_environments.tenant_id", "execution_environments.id"],
            name="fk_environment_leases_tenant_environment",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment_id", "environment_session_id"],
            ["environment_sessions.tenant_id", "environment_sessions.environment_id", "environment_sessions.id"],
            name="fk_environment_leases_tenant_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_environment_leases_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_environment_leases_idempotency"),
    )
    for column in (
        "tenant_id",
        "environment_id",
        "environment_session_id",
        "runtime_task_id",
        "agent_id",
        "status",
        "expires_at",
    ):
        op.create_index(f"ix_environment_leases_{column}", "environment_leases", [column])
    op.create_index(
        "ix_environment_leases_tenant_status_expiry",
        "environment_leases",
        ["tenant_id", "status", "expires_at"],
    )
    op.create_index(
        "uq_environment_leases_active_writer",
        "environment_leases",
        ["environment_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND access_mode = 'read_write'"),
    )

    op.create_table(
        "environment_checkpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("parent_checkpoint_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_runtime_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider_checkpoint_ref", sa.Text(), nullable=True),
        sa.Column("workspace_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_receipt_ref", sa.Text(), nullable=True),
        sa.Column("redacted_error", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_environment_checkpoints_generation_positive"),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed', 'deleted')",
            name="ck_environment_checkpoints_status",
        ),
        sa.CheckConstraint(
            "char_length(workspace_manifest_hash) = 64",
            name="ck_environment_checkpoints_workspace_hash",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_runtime_task_id"], ["runtime_tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment_id"],
            ["execution_environments.tenant_id", "execution_environments.id"],
            name="fk_environment_checkpoints_tenant_environment",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment_id", "environment_session_id"],
            ["environment_sessions.tenant_id", "environment_sessions.environment_id", "environment_sessions.id"],
            name="fk_environment_checkpoints_tenant_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment_id", "parent_checkpoint_id"],
            [
                "environment_checkpoints.tenant_id",
                "environment_checkpoints.environment_id",
                "environment_checkpoints.id",
            ],
            name="fk_environment_checkpoints_tenant_parent",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_environment_checkpoints_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "environment_id",
            "id",
            name="uq_environment_checkpoints_tenant_environment_id",
        ),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_environment_checkpoints_idempotency"),
    )
    for column in (
        "tenant_id",
        "environment_id",
        "environment_session_id",
        "source_runtime_task_id",
        "status",
    ):
        op.create_index(f"ix_environment_checkpoints_{column}", "environment_checkpoints", [column])
    op.create_index(
        "ix_environment_checkpoints_lineage",
        "environment_checkpoints",
        ["environment_id", "created_at"],
    )
    op.create_index(
        "ix_environment_checkpoints_tenant_status",
        "environment_checkpoints",
        ["tenant_id", "status", "retention_until"],
    )

    op.create_foreign_key(
        "fk_execution_environments_tenant_source_checkpoint",
        "execution_environments",
        "environment_checkpoints",
        ["tenant_id", "source_checkpoint_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_execution_environments_tenant_current_checkpoint",
        "execution_environments",
        "environment_checkpoints",
        ["tenant_id", "current_checkpoint_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL",
    )

    runtime_refs = (
        ("environment_id", "execution_environments", "fk_runtime_tasks_environment_id"),
        ("environment_session_id", "environment_sessions", "fk_runtime_tasks_environment_session_id"),
        ("environment_lease_id", "environment_leases", "fk_runtime_tasks_environment_lease_id"),
        ("environment_checkpoint_id", "environment_checkpoints", "fk_runtime_tasks_environment_checkpoint_id"),
    )
    for column, target, constraint in runtime_refs:
        op.add_column("runtime_tasks", sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(constraint, "runtime_tasks", target, [column], ["id"], ondelete="SET NULL")
        op.create_index(f"ix_runtime_tasks_{column}", "runtime_tasks", [column])

    for table in _TABLES:
        _enable_strict_tenant_rls(table)
    _install_tenant_binding_triggers()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_runtime_tasks_environment_binding ON runtime_tasks")
    op.execute("DROP FUNCTION IF EXISTS public.enforce_runtime_task_environment_binding()")
    for table in ("execution_environments", "environment_leases", "environment_checkpoints"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_tenant_binding ON {table}")
    op.execute("DROP FUNCTION IF EXISTS public.enforce_environment_tenant_binding()")

    runtime_refs = (
        ("environment_checkpoint_id", "fk_runtime_tasks_environment_checkpoint_id"),
        ("environment_lease_id", "fk_runtime_tasks_environment_lease_id"),
        ("environment_session_id", "fk_runtime_tasks_environment_session_id"),
        ("environment_id", "fk_runtime_tasks_environment_id"),
    )
    for column, constraint in runtime_refs:
        op.drop_index(f"ix_runtime_tasks_{column}", table_name="runtime_tasks")
        op.drop_constraint(constraint, "runtime_tasks", type_="foreignkey")
        op.drop_column("runtime_tasks", column)

    op.drop_constraint(
        "fk_execution_environments_tenant_current_checkpoint",
        "execution_environments",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_execution_environments_tenant_source_checkpoint",
        "execution_environments",
        type_="foreignkey",
    )
    op.drop_table("environment_leases")
    op.drop_table("environment_checkpoints")
    op.drop_table("environment_sessions")
    op.drop_table("execution_environments")
