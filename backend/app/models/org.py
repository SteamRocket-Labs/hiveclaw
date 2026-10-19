"""Organization directory mirrors and digital employee relationships."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OrgDepartment(Base):
    """An observed department in a provider's directory namespace."""

    __tablename__ = "org_departments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider_id", "provider_department_id", name="uq_org_department_provider_subject"),
        CheckConstraint("(provider_id IS NULL) = (provider_department_id IS NULL)", name="ck_org_department_provider_subject"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    feishu_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    provider_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("identity_providers.id"))
    provider_department_id: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_departments.id"))
    path: Mapped[str] = mapped_column(String(500), default="")
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    members: Mapped[list["OrgMember"]] = relationship(back_populates="department")


class OrgMember(Base):
    """An observed person, without an implied application account or role."""

    __tablename__ = "org_members"
    __table_args__ = (
        CheckConstraint(
            "observed_directory_department_ids IS NULL OR ("
            "jsonb_typeof(observed_directory_department_ids) = 'array' "
            "AND jsonb_array_length(observed_directory_department_ids) >= 1)",
            name="ck_org_members_observed_directory_departments",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("identity_providers.id"))
    external_id: Mapped[str | None] = mapped_column(String(100), index=True)
    open_id: Mapped[str | None] = mapped_column(String(100), index=True)
    unionid: Mapped[str | None] = mapped_column(String(100), index=True)
    feishu_open_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    feishu_user_id: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(200), default="")
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_departments.id"))
    # NULL means no current observation. One id replaces ambiguity. Several ids
    # remain authoritative until a new observation or an explicit repair.
    observed_directory_department_ids: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    department_path: Mapped[str] = mapped_column(String(500), default="")
    phone: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="active")
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    department: Mapped["OrgDepartment | None"] = relationship(back_populates="members")


class AgentRelationship(Base):
    """Relationship between an agent and an org member."""

    __tablename__ = "agent_relationships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_members.id"), nullable=False)
    relation: Mapped[str] = mapped_column(String(50), nullable=False, default="collaborator")
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    member: Mapped["OrgMember"] = relationship()
