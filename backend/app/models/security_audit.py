"""Security audit event and resource permission models."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SecurityAuditEvent(Base):
    """Append-only security audit log with hash chain for tamper detection."""

    __tablename__ = "security_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sequence_num: Mapped[int | None] = mapped_column(BigInteger, server_default=None, unique=True, nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="info")
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(30))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # Execution Identity (Block C) — who triggered/is responsible for this action
    execution_identity_type: Mapped[str | None] = mapped_column(String(20))  # agent_bot | delegated_user
    execution_identity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    execution_identity_label: Mapped[str | None] = mapped_column(String(200))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResourcePermission(Base):
    """Fine-grained resource-level permission (RBAC + ABAC conditions)."""

    __tablename__ = "resource_permissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    principal_type: Mapped[str] = mapped_column(String(20), nullable=False)  # user, role, department, agent
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(30), nullable=False)  # agent, tool, skill, llm_model
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actions: Mapped[list] = mapped_column(ARRAY(Text), nullable=False)  # ['read', 'execute', 'configure', 'manage']
    conditions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # ABAC conditions
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
