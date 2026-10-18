"""Persistent project organization; referenced resources retain their own truth and ACL."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WorkbenchProject(Base):
    __tablename__ = "workbench_projects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_workbench_project_tenant_id"),
        CheckConstraint("revision > 0", name="ck_workbench_project_revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WorkbenchProjectEntry(Base):
    __tablename__ = "workbench_project_entries"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "project_id"], ["workbench_projects.tenant_id", "workbench_projects.id"], ondelete="CASCADE"),
        CheckConstraint("target_kind IN ('workspace_file','session')", name="ck_workbench_project_entry_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    # Soft references preserve a recoverable link after a source is deleted.
    # Titles, bodies, paths, versions and permissions belong to the source.
    target_kind: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    section: Mapped[str] = mapped_column(String(100), default="")
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
