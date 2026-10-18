"""Durable private command-resource identity and owned execution leases."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ExecutionEnvironment(Base):
    __tablename__ = "execution_environments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", name="uq_execution_environment_private_agent"),
        UniqueConstraint("tenant_id", "id", name="uq_execution_environment_tenant_id"),
        CheckConstraint("desired_state IN ('ready','stopped')", name="ck_execution_environment_desired"),
        CheckConstraint(
            "observed_state IN ('ready','stopping','stopped','lost','failed')", name="ck_execution_environment_observed"
        ),
        CheckConstraint(
            "generation > 0 AND revision > 0 AND fence_counter >= 0", name="ck_execution_environment_versions"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    desired_state: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    observed_state: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    fence_counter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EnvironmentLease(Base):
    __tablename__ = "environment_leases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "environment_id"],
            ["execution_environments.tenant_id", "execution_environments.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("status IN ('active','released')", name="ck_environment_lease_status"),
        CheckConstraint("generation > 0 AND fence_version > 0", name="ck_environment_lease_versions"),
        Index("uq_environment_active_writer", "environment_id", unique=True, postgresql_where=text("status='active'")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="SET NULL")
    )
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    invocation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    fence_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
