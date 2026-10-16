"""Per-user, per-company pointer to My Assistant.

The row is not an access grant. Ownership, execution, model use, and private
history stay on the native employee and the existing permission checks.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserAssistantBinding(Base):
    __tablename__ = "user_assistant_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_user_assistant_bindings_tenant_user"),
        CheckConstraint(
            "status IN ('active','provisioning','provision_incomplete')",
            name="ck_user_assistant_bindings_status",
        ),
        CheckConstraint(
            "source IN ('explicit_selection','provisioned','legacy_provenance')",
            name="ck_user_assistant_bindings_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    prior_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # Snapshot of responsibility at bind time. No FK: a later transfer must not
    # erase the fact, and this column does not grant that user anything.
    owner_user_id_at_bind: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provision_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
