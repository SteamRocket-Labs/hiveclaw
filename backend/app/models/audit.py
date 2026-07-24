"""Audit log, approval request, chat message, and enterprise info models."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditLog(Base):
    """Append-only audit trail for all operations."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    external_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "external_principals.id",
            name="fk_audit_logs_external_principal_id",
            ondelete="RESTRICT",
        ),
        index=True,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"))
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default={})
    ip_address: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


@event.listens_for(AuditLog, "before_insert")
def _bind_audit_log_external_execution_identity(_mapper, _connection, target: AuditLog) -> None:
    if target.external_principal_id is not None:
        return
    from app.core.execution_context import get_execution_identity

    identity = get_execution_identity()
    if identity is None or identity.identity_type not in {
        "external_principal",
        "external_principal_bound",
    }:
        return
    if identity.identity_id is not None:
        target.external_principal_id = identity.identity_id


class ApprovalRequest(Base):
    """Approval request for capability-gated operations."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint(
            "execution_idempotency_key",
            name="uq_approval_requests_execution_idempotency_key",
        ),
        CheckConstraint(
            "execution_status IN ('pending','approved','rejected','queued','executing','succeeded','failed',"
            "'needs_reconciliation','needs_reapproval')",
            name="ck_approval_requests_execution_status",
        ),
        UniqueConstraint(
            "execution_task_id",
            name="uq_approval_requests_execution_task_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default={})
    status: Mapped[str] = mapped_column(
        Enum("pending", "approved", "rejected", name="approval_status_enum"),
        default="pending",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    requested_by_external_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("external_principals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    decision_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    normalized_arguments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    policy_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    policy_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_envelope: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    execution_envelope_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    execution_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "runtime_tasks.id",
            name="fk_approval_requests_execution_task_id_runtime_tasks",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    execution_idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    execution_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ChatMessage(Base):
    """Web chat message between user and agent."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index(
            "uq_chat_messages_ingress_user",
            "source_ingress_event_id",
            unique=True,
            postgresql_where=text("source_ingress_event_id IS NOT NULL"),
            sqlite_where=text("source_ingress_event_id IS NOT NULL"),
        ),
        Index("ix_chat_messages_conversation_created_at", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    external_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("external_principals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    role: Mapped[str] = mapped_column(
        Enum("user", "assistant", "system", "tool_call", name="chat_role_enum"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(200), default="web", nullable=False)
    # Participant identity (unified User/Agent identity)
    participant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("participants.id"), nullable=True
    )
    # Model thinking process
    thinking: Mapped[str | None] = mapped_column(Text, nullable=True)
    thinking_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source_ingress_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("channel_ingress_events.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


@event.listens_for(ChatMessage, "before_insert")
def _bind_chat_message_to_channel_ingress(_mapper, _connection, target: ChatMessage) -> None:
    if target.role != "user" or target.source_ingress_event_id is not None:
        return
    from app.services.channel_ingress_context import current_channel_ingress_context

    ingress = current_channel_ingress_context()
    if ingress is None:
        return
    if target.tenant_id != ingress.tenant_id or target.agent_id != ingress.agent_id:
        raise ValueError("channel ingress message authority does not match the claimed event")
    target.source_ingress_event_id = ingress.event_id


class EnterpriseInfo(Base):
    """Centralized enterprise information with versioning for sync."""

    __tablename__ = "enterprise_info"
    __table_args__ = (UniqueConstraint("tenant_id", "info_type", name="uq_enterprise_info_tenant_info_type"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    info_type: Mapped[str] = mapped_column(String(50), nullable=False)  # org_structure, company_profile, etc.
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    visible_roles: Mapped[list] = mapped_column(JSON, default=[])  # Which agent roles can see this
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# Keep isolated model imports mapper-safe for external/channel evidence FKs.
from app.models.channel_ingress_event import ChannelIngressEvent as _ChannelIngressEvent  # noqa: E402, F401
from app.models.external_principal import ExternalPrincipal as _ExternalPrincipal  # noqa: E402, F401
