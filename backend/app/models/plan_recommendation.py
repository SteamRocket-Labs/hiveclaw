"""Plan Mode recommendation ledger.

Recommendation records bind the "recommend Plan Mode" UX step to a real user,
agent, and session. A later opt-out can only bypass the autonomous-wake gate
when it references one of these rows after the user has declined it.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentPlanRecommendation(Base):
    """A user-visible Plan Mode recommendation and its decision state."""

    __tablename__ = "agent_plan_recommendations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    recommended_to_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )

    source: Mapped[str] = mapped_column(String(30), nullable=False, default="web_chat")
    intent_type: Mapped[str] = mapped_column(String(30), nullable=False, default="autonomous_wake")
    action_kind: Mapped[str] = mapped_column(String(50), nullable=False, default="create_enabled_trigger")
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False, default="set_trigger")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    original_request: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # recommended | declined | accepted | expired
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="recommended")
    declined_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_agent_plan_recommendations_agent_status", "agent_id", "status"),
        Index("ix_agent_plan_recommendations_session_status", "session_id", "status"),
        Index(
            "ix_agent_plan_recommendations_user_session",
            "recommended_to_user_id",
            "session_id",
        ),
    )
