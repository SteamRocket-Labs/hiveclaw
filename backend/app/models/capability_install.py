"""Persistent install records for agent capabilities.

Tracks per-agent capability provisioning status for platform skills,
ClawHub skills, and MCP servers so HR-created agents can surface a
truthful readiness state instead of a best-effort success string.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentCapabilityInstall(Base):
    """Install state for one requested capability on one agent."""

    __tablename__ = "agent_capability_installs"
    __table_args__ = (
        UniqueConstraint("agent_id", "kind", "normalized_key", name="uq_agent_capability_install"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # platform_skill | mcp_server | clawhub_skill
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_key: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True, default="pending")
    installed_via: Mapped[str] = mapped_column(String(30), nullable=False, default="hr_agent")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
