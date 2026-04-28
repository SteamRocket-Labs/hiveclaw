"""Activity log model for tracking agent actions."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentActivityLog(Base):
    """Records every action taken by a digital employee."""

    __tablename__ = "agent_activity_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(
        Enum(
            "chat_reply", "tool_call", "feishu_msg_sent", "agent_msg_sent",
            "web_msg_sent", "task_created", "task_updated", "file_written", "error",
            "schedule_run", "heartbeat", "plaza_post",
            "delegation_started", "delegation_completed", "delegation_failed",
            "delegation_killed", "delegation_error",
            "tool_call_direct", "tool_call_approved",
            "channel_delivery_success", "channel_delivery_failed",
            "channel_delivery_unavailable", "channel_capability_denied",
            name="activity_action_enum",
            create_constraint=False,
        ),
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    detail_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    related_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
