"""Durable mechanical coverage ledger for one root execution tree.

The model records requested work units before they are fanned out.  It owns no
semantic judgment: the model decides what work to delegate, while this table
records whether each requested unit was admitted, deferred, or rejected and
which durable runtime/session/result facts belong to it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RuntimeRootItem(Base):
    __tablename__ = "runtime_root_items"
    __table_args__ = (
        CheckConstraint(
            "state IN ('requested', 'waiting_approval', 'queued', 'running', 'completed', "
            "'failed', 'killed', 'skipped', 'cancelled', 'suspended', "
            "'needs_reconciliation', 'not_admitted')",
            name="ck_runtime_root_items_state",
        ),
        CheckConstraint(
            "admission_disposition IN ('requested', 'admitted', 'deferred', 'not_admitted')",
            name="ck_runtime_root_items_admission_disposition",
        ),
        UniqueConstraint(
            "tenant_id",
            "root_runtime_task_id",
            "intent_key",
            name="uq_runtime_root_items_root_intent",
        ),
        Index(
            "ix_runtime_root_items_root_coverage",
            "tenant_id",
            "root_runtime_task_id",
            "admission_disposition",
            "state",
        ),
        Index(
            "ix_runtime_root_items_team_recovery",
            "work_type",
            "state",
            "runtime_task_id",
            "next_recovery_at",
            "recovery_claim_expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    root_runtime_task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    parent_runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    root_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    root_session_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)

    intent_key: Mapped[str] = mapped_column(String(300), nullable=False)
    work_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    path_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'"))

    state: Mapped[str] = mapped_column(String(40), nullable=False, default="requested", index=True)
    admission_disposition: Mapped[str] = mapped_column(String(24), nullable=False, default="requested", index=True)
    reason_code: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    budget_reservation_key: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    approval_ref: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    child_session_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    result_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'"))
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'"))
    recovery_claimed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    recovery_claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    next_recovery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Mapper-safe import for the optional runtime-task FK.
from app.models.runtime_task import RuntimeTask  # noqa: E402, F401
