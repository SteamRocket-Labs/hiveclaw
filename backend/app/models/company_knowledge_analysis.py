"""Recoverable evidence analysis and immutable, non-authoritative candidates."""

from datetime import datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CompanyKnowledgeAnalysisJob(Base):
    __tablename__ = "company_knowledge_analysis_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_hash", name="uq_company_analysis_request"),
        CheckConstraint(
            "status IN ('queued','running','completed','held','failed','cancelled')", name="ck_company_analysis_status"
        ),
        CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_company_analysis_attempts"),
        Index("ix_company_analysis_claim", "tenant_id", "status", "available_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_knowledge_evidence.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_documents.id", ondelete="RESTRICT")
    )
    namespace: Mapped[str] = mapped_column(String(300), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    accountable_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued", server_default="queued")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    error_code: Mapped[str | None] = mapped_column(String(120))
    dispatch_receipt_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CompanyKnowledgeExtraction(Base):
    __tablename__ = "company_knowledge_extractions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "job_id", "attempt", name="uq_company_extraction_attempt"),
        Index(
            "uq_company_extraction_accepted",
            "tenant_id",
            "job_id",
            unique=True,
            postgresql_where=text("validated = true"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_knowledge_analysis_jobs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_knowledge_evidence.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model_receipt_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
