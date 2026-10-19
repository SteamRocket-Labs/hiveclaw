"""Persistent directory identity, business teams, cutover journal, and receipts.

These tables record the associations in the organization contract. They do
not grant access, change roles, or replace OrgMember and User.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

_SUBJECT_KINDS = "('provider_user_id','provider_open_id','provider_union_id')"
_AUDIENCE_KINDS = "('private','group','business_team')"
# CHECK treats NULL as success, so every required JSON test is COALESCE'd to false.
LINK_EVIDENCE_CHECK = (
    "evidence_json IS NOT NULL "
    "AND jsonb_typeof(evidence_json) = 'object' "
    "AND COALESCE(jsonb_typeof(evidence_json->'source') = 'string', false) "
    "AND COALESCE(evidence_json->>'source' IN ('org_sync','sso','manual','dry_run'), false) "
    "AND COALESCE(jsonb_typeof(evidence_json->'observed_at') = 'string', false) "
    "AND COALESCE(length(btrim(evidence_json->>'observed_at')) > 0, false) "
    "AND (COALESCE(evidence_json->>'source' <> 'manual', false) OR created_by_user_id IS NOT NULL)"
)


class OrgMemberUserLink(Base):
    """One stored relationship between a directory person and a login account."""

    __tablename__ = "org_member_user_links"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','conflict','inactive_user','revoked')",
            name="ck_org_member_user_links_status",
        ),
        CheckConstraint(
            f"subject_kind IS NULL OR subject_kind IN {_SUBJECT_KINDS}",
            name="ck_org_member_user_links_subject_kind",
        ),
        CheckConstraint(
            "match_subject <> ''",
            name="ck_org_member_user_links_match_subject",
        ),
        CheckConstraint(
            "("
            "status <> 'active' OR ("
            "provider_id IS NOT NULL AND subject_kind IS NOT NULL AND match_subject <> ''"
            ")"
            ")",
            name="ck_org_member_user_links_active_requires_subject",
        ),
        CheckConstraint(LINK_EVIDENCE_CHECK, name="ck_org_member_user_links_evidence"),
        Index(
            "uq_org_member_user_links_active_subject",
            "tenant_id",
            "provider_id",
            "subject_kind",
            "match_subject",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_org_member_user_links_active_member_kind",
            "tenant_id",
            "org_member_id",
            "provider_id",
            "subject_kind",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("org_members.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity_providers.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    subject_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    match_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    external_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("external_identities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )


class DepartmentDirectoryLink(Base):
    """Optional alignment between a mirrored directory department and an internal department."""

    __tablename__ = "department_directory_links"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','conflict','revoked')",
            name="ck_department_directory_links_status",
        ),
        CheckConstraint(
            "provider IN ('feishu','dingtalk','wecom')",
            name="ck_department_directory_links_provider",
        ),
        CheckConstraint("provider = 'feishu' OR provider_id IS NOT NULL",
            name="ck_department_directory_links_namespace"),
        CheckConstraint(
            "provider_department_id <> ''",
            name="ck_department_directory_links_provider_department",
        ),
        Index(
            "uq_department_directory_links_active_org_department",
            "tenant_id",
            "org_department_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_department_directory_links_active_provider_department",
            "tenant_id",
            "provider",
            "provider_department_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_department_directory_links_active_department",
            "tenant_id",
            "provider",
            "department_id",
            unique=True,
            postgresql_where=text("status = 'active' AND department_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("org_departments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity_providers.id", ondelete="RESTRICT"), nullable=True
    )
    provider_department_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class BusinessTeam(Base):
    """A business team. This is not an AgentTeam and not a directory department."""

    __tablename__ = "business_teams"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','suspended','archived')",
            name="ck_business_teams_status",
        ),
        CheckConstraint("name <> ''", name="ck_business_teams_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True, index=True
    )


class BusinessTeamMembership(Base):
    """Membership in a business team. An active user row is the only one that can later carry permission."""

    __tablename__ = "business_team_memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('member','lead')",
            name="ck_business_team_memberships_role",
        ),
        CheckConstraint(
            "status IN ('active','left','suspended')",
            name="ck_business_team_memberships_status",
        ),
        CheckConstraint(
            "user_id IS NOT NULL OR org_member_id IS NOT NULL",
            name="ck_business_team_memberships_person",
        ),
        Index(
            "uq_business_team_memberships_active_user",
            "business_team_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active' AND user_id IS NOT NULL"),
        ),
        Index(
            "uq_business_team_memberships_active_org_member",
            "business_team_id",
            "org_member_id",
            unique=True,
            postgresql_where=text("status = 'active' AND org_member_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    business_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business_teams.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    org_member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("org_members.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdentityCutoverJournal(Base):
    """Append-only before/after facts for a link or later department cutover."""

    __tablename__ = "identity_cutover_journal"
    __table_args__ = (
        CheckConstraint("object_kind <> ''", name="ck_identity_cutover_journal_object_kind"),
        CheckConstraint(
            "jsonb_typeof(after_json) = 'object'",
            name="ck_identity_cutover_journal_after_json",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    object_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    before_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    after_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ContentInputReceipt(Base):
    """Receipt for one verified team-memory or workspace-artifact write."""

    __tablename__ = "content_input_receipts"
    __table_args__ = (
        CheckConstraint(
            "carrier_kind IN ('team_memory','workspace_artifact')",
            name="ck_content_input_receipts_carrier_kind",
        ),
        CheckConstraint("carrier_ref <> ''", name="ck_content_input_receipts_carrier_ref"),
        CheckConstraint(
            "length(content_sha256) = 64 AND content_sha256 = lower(content_sha256) "
            "AND translate(content_sha256, '0123456789abcdef', '') = ''",
            name="ck_content_input_receipts_content_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(dependencies_json) = 'array'",
            name="ck_content_input_receipts_dependencies_array",
        ),
        Index(
            "uq_content_input_receipts_revision",
            "tenant_id",
            "carrier_kind",
            "carrier_ref",
            "revision",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    carrier_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    carrier_ref: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dependencies_json: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )


AUDIENCE_KIND_CHECK = f"audience_kind IS NULL OR audience_kind IN {_AUDIENCE_KINDS}"
SESSION_AUDIENCE_CHECK = (
    f"({AUDIENCE_KIND_CHECK}) AND ("
    "(audience_kind = 'business_team' AND audience_ref IS NOT NULL) OR "
    "(audience_kind IS NULL AND audience_ref IS NULL) OR "
    "(audience_kind IN ('private','group') AND audience_ref IS NULL)"
    ")"
)
