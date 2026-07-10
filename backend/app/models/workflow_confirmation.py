"""Durable Dynamic Workflow proposal and preview confirmation artifacts."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WorkflowProposalArtifact(Base):
    """Canonical, session-bound Dynamic Workflow proposal."""

    __tablename__ = "workflow_proposal_artifacts"
    __table_args__ = (
        CheckConstraint("status IN ('open','previewed','expired')", name="ck_workflow_proposal_artifact_status"),
        Index("ix_workflow_proposal_identity", "tenant_id", "agent_id", "session_id", "requested_by_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open", server_default="open")
    artifact_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    artifact_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    proposal_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkflowPreviewArtifact(Base):
    """Immutable canonical definition/args snapshot and exactly-once start ledger."""

    __tablename__ = "workflow_preview_artifacts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ready','starting','started','failed','expired')",
            name="ck_workflow_preview_artifact_status",
        ),
        Index("ix_workflow_preview_identity", "tenant_id", "agent_id", "session_id", "requested_by_user_id"),
        Index("ix_workflow_preview_start_claim", "status", "claim_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_proposal_artifacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    candidate_id: Mapped[str | None] = mapped_column(String(160), nullable=True)

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ready", server_default="ready")
    artifact_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    artifact_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    args_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    definition_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    args_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    preview_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmation_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmation_evidence_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, unique=True)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
