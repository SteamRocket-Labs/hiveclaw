"""Security audit event and resource permission models."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
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
    __table_args__ = (
        Index(
            "ix_resource_permissions_principal_key_resource_key",
            "tenant_id",
            "principal_type",
            "principal_key",
            "resource_type",
            "resource_key",
        ),
        CheckConstraint(
            "principal_id IS NOT NULL OR principal_key IS NOT NULL",
            name="ck_resource_permissions_principal_ref",
        ),
        CheckConstraint(
            "resource_id IS NOT NULL OR resource_key IS NOT NULL",
            name="ck_resource_permissions_resource_ref",
        ),
        CheckConstraint(
            "effect IN ('allow','deny')",
            name="ck_resource_permissions_effect",
        ),
        CheckConstraint(
            "sensitivity_ceiling IN ('PL1_public','PL2_pii','PL3_sensitive','PL4_credential')",
            name="ck_resource_permissions_sensitivity_ceiling",
        ),
        CheckConstraint(
            "revoked_by_user_id IS NULL OR revoked_at IS NOT NULL",
            name="ck_resource_permissions_revoke_actor",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    principal_type: Mapped[str] = mapped_column(String(20), nullable=False)  # user, role, department, agent
    principal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    principal_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resource_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    actions: Mapped[list] = mapped_column(ARRAY(Text), nullable=False)  # ['read', 'execute', 'configure', 'manage']
    conditions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # ABAC conditions
    effect: Mapped[str] = mapped_column(String(10), nullable=False, default="allow", server_default="allow")
    sensitivity_ceiling: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PL1_public", server_default="PL1_public"
    )
    purposes: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, default=list, server_default="{}")
    source_acl_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
