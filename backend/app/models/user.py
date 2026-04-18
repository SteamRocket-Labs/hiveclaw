"""User and organization models."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    """Platform user."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    role: Mapped[str] = mapped_column(
        Enum("platform_admin", "org_admin", "member", name="user_role_enum"),
        default="member",
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("departments.id"))
    title: Mapped[str | None] = mapped_column(String(100))

    # Feishu SSO
    feishu_open_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    feishu_union_id: Mapped[str | None] = mapped_column(String(255))
    feishu_user_id: Mapped[str | None] = mapped_column(String(255))

    # OIDC SSO
    oidc_sub: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    oidc_issuer: Mapped[str | None] = mapped_column(String(500))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set to True when the user was created by an SSO import (Feishu org sync etc.)
    # with a known default password. Login surfaces this as needs_password_change
    # so the UI can prompt the user to rotate away from the shared default.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Token quota (employee-level, covers all agents — the only usage limit)
    quota_tokens_per_day: Mapped[int | None] = mapped_column(Integer)
    quota_tokens_per_month: Mapped[int | None] = mapped_column(Integer)
    tokens_used_today: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used_month: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used_total: Mapped[int] = mapped_column(Integer, default=0)
    tokens_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    department: Mapped["Department | None"] = relationship(back_populates="members", foreign_keys=[department_id])
    created_agents: Mapped[list["Agent"]] = relationship(back_populates="creator", foreign_keys="Agent.creator_id")


class Department(Base):
    """Organization department (tree structure)."""

    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("departments.id"))
    manager_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    sort_order: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    parent: Mapped["Department | None"] = relationship(
        "Department", remote_side=[id], back_populates="children"
    )
    children: Mapped[list["Department"]] = relationship("Department", back_populates="parent")
    manager: Mapped["User | None"] = relationship("User", foreign_keys=[manager_id])
    members: Mapped[list["User"]] = relationship(
        "User", back_populates="department", foreign_keys="User.department_id"
    )


# Forward reference for Agent used in User relationship
from app.models.agent import Agent  # noqa: E402, F401
