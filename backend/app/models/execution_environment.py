"""Provider-neutral Agent execution environment authority models."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ExecutionEnvironment(Base):
    """Long-lived logical environment identity for one Agent or task fork."""

    __tablename__ = "execution_environments"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('agent_private', 'task_fork')",
            name="ck_execution_environments_scope_type",
        ),
        CheckConstraint(
            "desired_state IN ('running', 'stopped', 'destroyed')",
            name="ck_execution_environments_desired_state",
        ),
        CheckConstraint(
            "observed_state IN ('pending', 'starting', 'ready', 'recovering', 'stopping', "
            "'stopped', 'unavailable', 'failed', 'destroyed')",
            name="ck_execution_environments_observed_state",
        ),
        CheckConstraint("generation >= 1", name="ck_execution_environments_generation_positive"),
        CheckConstraint("row_version >= 1", name="ck_execution_environments_row_version_positive"),
        CheckConstraint(
            "char_length(policy_snapshot_hash) = 64",
            name="ck_execution_environments_policy_hash",
        ),
        CheckConstraint(
            "workspace_manifest_hash IS NULL OR char_length(workspace_manifest_hash) = 64",
            name="ck_execution_environments_workspace_hash",
        ),
        CheckConstraint(
            "(scope_type = 'agent_private' AND parent_environment_id IS NULL "
            "AND source_checkpoint_id IS NULL AND owner_runtime_task_id IS NULL) OR "
            "(scope_type = 'task_fork' AND parent_environment_id IS NOT NULL "
            "AND source_checkpoint_id IS NOT NULL AND owner_runtime_task_id IS NOT NULL)",
            name="ck_execution_environments_scope_binding",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_execution_environments_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "parent_environment_id"],
            ["execution_environments.tenant_id", "execution_environments.id"],
            name="fk_execution_environments_tenant_parent",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_checkpoint_id"],
            ["environment_checkpoints.tenant_id", "environment_checkpoints.id"],
            name="fk_execution_environments_tenant_source_checkpoint",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["tenant_id", "current_checkpoint_id"],
            ["environment_checkpoints.tenant_id", "environment_checkpoints.id"],
            name="fk_execution_environments_tenant_current_checkpoint",
            ondelete="SET NULL",
            use_alter=True,
        ),
        Index(
            "uq_execution_environments_agent_private",
            "tenant_id",
            "agent_id",
            unique=True,
            postgresql_where=text("scope_type = 'agent_private' AND deleted_at IS NULL"),
            sqlite_where=text("scope_type = 'agent_private' AND deleted_at IS NULL"),
        ),
        Index(
            "ix_execution_environments_tenant_state",
            "tenant_id",
            "observed_state",
            "last_used_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(24), nullable=False, default="agent_private")
    parent_environment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_checkpoint_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    owner_runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    desired_state: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    observed_state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    capability_profile: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    policy_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_checkpoint_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idle_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __mapper_args__ = {"version_id_col": row_version}


class EnvironmentSession(Base):
    """One provider compute generation attached to a logical environment."""

    __tablename__ = "environment_sessions"
    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_environment_sessions_generation_positive"),
        CheckConstraint(
            "state IN ('starting', 'ready', 'recovering', 'stopping', 'stopped', "
            "'unavailable', 'failed', 'destroyed')",
            name="ck_environment_sessions_state",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_environment_sessions_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "environment_id",
            "id",
            name="uq_environment_sessions_tenant_environment_id",
        ),
        UniqueConstraint("environment_id", "generation", name="uq_environment_sessions_generation"),
        ForeignKeyConstraint(
            ["tenant_id", "environment_id"],
            ["execution_environments.tenant_id", "execution_environments.id"],
            name="fk_environment_sessions_tenant_environment",
            ondelete="CASCADE",
        ),
        Index(
            "uq_environment_sessions_current_writable",
            "environment_id",
            unique=True,
            postgresql_where=text("state IN ('starting', 'ready', 'recovering')"),
            sqlite_where=text("state IN ('starting', 'ready', 'recovering')"),
        ),
        Index("ix_environment_sessions_tenant_state", "tenant_id", "state", "last_observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_resource_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_session_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="starting", index=True)
    capability_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_receipt_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    redacted_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))


class EnvironmentLease(Base):
    """Fenced RuntimeTask attachment to one writable environment generation."""

    __tablename__ = "environment_leases"
    __table_args__ = (
        CheckConstraint(
            "access_mode IN ('read_only', 'read_write')",
            name="ck_environment_leases_access_mode",
        ),
        CheckConstraint(
            "status IN ('active', 'released', 'expired', 'revoked')",
            name="ck_environment_leases_status",
        ),
        CheckConstraint("fence_version >= 1", name="ck_environment_leases_fence_positive"),
        UniqueConstraint("tenant_id", "id", name="uq_environment_leases_tenant_id_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_environment_leases_idempotency"),
        ForeignKeyConstraint(
            ["tenant_id", "environment_id"],
            ["execution_environments.tenant_id", "execution_environments.id"],
            name="fk_environment_leases_tenant_environment",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "environment_id", "environment_session_id"],
            ["environment_sessions.tenant_id", "environment_sessions.environment_id", "environment_sessions.id"],
            name="fk_environment_leases_tenant_session",
            ondelete="CASCADE",
        ),
        Index(
            "uq_environment_leases_active_writer",
            "environment_id",
            unique=True,
            postgresql_where=text("status = 'active' AND access_mode = 'read_write'"),
            sqlite_where=text("status = 'active' AND access_mode = 'read_write'"),
        ),
        Index("ix_environment_leases_tenant_status_expiry", "tenant_id", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    environment_session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    runtime_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    access_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="read_write")
    fence_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EnvironmentCheckpoint(Base):
    """Recoverable provider snapshot tied to canonical workspace evidence."""

    __tablename__ = "environment_checkpoints"
    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_environment_checkpoints_generation_positive"),
        CheckConstraint(
            "status IN ('pending', 'ready', 'failed', 'deleted')",
            name="ck_environment_checkpoints_status",
        ),
        CheckConstraint(
            "char_length(workspace_manifest_hash) = 64",
            name="ck_environment_checkpoints_workspace_hash",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_environment_checkpoints_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "environment_id",
            "id",
            name="uq_environment_checkpoints_tenant_environment_id",
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_environment_checkpoints_idempotency"),
        ForeignKeyConstraint(
            ["tenant_id", "environment_id"],
            ["execution_environments.tenant_id", "execution_environments.id"],
            name="fk_environment_checkpoints_tenant_environment",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "environment_id", "environment_session_id"],
            ["environment_sessions.tenant_id", "environment_sessions.environment_id", "environment_sessions.id"],
            name="fk_environment_checkpoints_tenant_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "environment_id", "parent_checkpoint_id"],
            [
                "environment_checkpoints.tenant_id",
                "environment_checkpoints.environment_id",
                "environment_checkpoints.id",
            ],
            name="fk_environment_checkpoints_tenant_parent",
            ondelete="RESTRICT",
        ),
        Index("ix_environment_checkpoints_lineage", "environment_id", "created_at"),
        Index("ix_environment_checkpoints_tenant_status", "tenant_id", "status", "retention_until"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    environment_session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_checkpoint_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider_checkpoint_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_receipt_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    redacted_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
