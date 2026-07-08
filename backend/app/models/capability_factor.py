from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CapabilityFactor(Base):
    """Reusable capability candidate captured before enterprise catalog promotion."""

    __tablename__ = "capability_factors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    originating_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    originating_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    factor_kind: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    trace_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    artifact_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    upstream_source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_content_sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    license_report_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    display_name: Mapped[str] = mapped_column(String(260), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    authoring_contract_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    declared_components_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    declared_permissions_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sensitivity_report_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reuse_score_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    suggested_scope: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="captured", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CapabilityFactorReview(Base):
    """Automated or human review evidence for one capability factor."""

    __tablename__ = "capability_factor_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    factor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("capability_factors.id", ondelete="CASCADE"), index=True
    )
    automated_report_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    admission_report_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    findings_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    dedupe_report_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    eval_report_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    decision: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CapabilityPromotionProposal(Base):
    """Proposal to promote a reviewed factor into a trusted extension snapshot/catalog path."""

    __tablename__ = "capability_promotion_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    factor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("capability_factors.id", ondelete="CASCADE"), index=True
    )
    review_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("capability_factor_reviews.id", ondelete="SET NULL"), nullable=True, index=True
    )
    proposed_snapshot_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    proposed_catalog_scope: Mapped[str] = mapped_column(String(40), nullable=False, default="workspace")
    proposed_activation_policy: Mapped[str] = mapped_column(String(60), nullable=False, default="requestable")
    proposed_selector_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    decision: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resulting_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("external_capability_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
