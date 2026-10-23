"""Private browser resources; task and canonical file truth remain elsewhere."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EnvironmentBrowser(Base):
    __tablename__ = "environment_browsers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "environment_id"],
            ["execution_environments.tenant_id", "execution_environments.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('starting','ready','lost','stopped','revoked')", name="ck_environment_browser_status"
        ),
        CheckConstraint(
            "control_mode IN ('agent','requested','human','paused')", name="ck_environment_browser_control"
        ),
        CheckConstraint(
            "desired_state IN ('running','stopped') AND generation > 0", name="ck_environment_browser_desired"
        ),
        CheckConstraint(
            "revision > 0 AND control_epoch > 0 AND observed_epoch >= 0", name="ck_environment_browser_versions"
        ),
        Index(
            "uq_environment_browser_active",
            "environment_id",
            unique=True,
            postgresql_where=text("status IN ('starting','ready','lost')"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="SET NULL"), index=True
    )
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="starting")
    desired_state: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    control_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="agent")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    control_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    observed_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    control_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    viewer_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_instance: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_nonce: Mapped[str | None] = mapped_column(String(64))
    lifecycle_secret: Mapped[str | None] = mapped_column(Text)
    lifecycle_receipt_path: Mapped[str | None] = mapped_column(Text)
    resource_error: Mapped[str | None] = mapped_column(String(128))
    last_effect: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
