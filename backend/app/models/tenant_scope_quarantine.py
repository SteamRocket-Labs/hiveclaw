"""Durable evidence for legacy rows whose tenant authority cannot be derived."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TenantScopeQuarantineRecord(Base):
    """Operator-only receipt for one row isolated under the quarantine tenant."""

    __tablename__ = "tenant_scope_quarantine_records"
    __table_args__ = (UniqueConstraint("source_table", "source_row_id", name="uq_tenant_scope_quarantine_source"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_table: Mapped[str] = mapped_column(String(100), nullable=False)
    source_row_id: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
