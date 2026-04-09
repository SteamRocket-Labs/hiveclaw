"""Participant identity model — unified identity for Users and Agents."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Participant(Base):
    """Lightweight identity that unifies Users and Agents as first-class participants.

    Used by ChatSession, ChatMessage, and future approval/collaboration features.
    type: 'user' | 'agent'
    ref_id: points to users.id or agents.id
    """

    __tablename__ = "participants"
    __table_args__ = (
        UniqueConstraint("type", "ref_id", name="uq_participants_type_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(10), nullable=False)  # 'user' | 'agent'
    ref_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
