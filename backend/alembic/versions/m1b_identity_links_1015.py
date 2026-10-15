"""Add persistent identity links, business-team schema, and nullable audience columns.

Revision ID: m1b_identity_links_1015
Revises: session_v2_delete_fk_indexes_0907
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m1b_identity_links_1015"
down_revision = "session_v2_delete_fk_indexes_0907"
branch_labels = None
depends_on = None

IDENTITY_LINK_TABLES = (
    "org_member_user_links",
    "department_directory_links",
    "business_teams",
    "business_team_memberships",
    "identity_cutover_journal",
    "content_input_receipts",
)

_LINK_EVIDENCE_SQL = (
    "evidence_json IS NOT NULL "
    "AND jsonb_typeof(evidence_json) = 'object' "
    "AND COALESCE(jsonb_typeof(evidence_json->'source') = 'string', false) "
    "AND COALESCE(evidence_json->>'source' IN ('org_sync','sso','manual','dry_run'), false) "
    "AND COALESCE(jsonb_typeof(evidence_json->'observed_at') = 'string', false) "
    "AND COALESCE(length(btrim(evidence_json->>'observed_at')) > 0, false) "
    "AND (COALESCE(evidence_json->>'source' <> 'manual', false) OR created_by_user_id IS NOT NULL)"
)
_AUDIENCE_KIND_SQL = "audience_kind IS NULL OR audience_kind IN ('private','group','business_team')"
_SESSION_AUDIENCE_SQL = (
    f"({_AUDIENCE_KIND_SQL}) AND ("
    "(audience_kind = 'business_team' AND audience_ref IS NOT NULL) OR "
    "(audience_kind IS NULL AND audience_ref IS NULL) OR "
    "(audience_kind IN ('private','group') AND audience_ref IS NULL)"
    ")"
)
_STRICT_PREDICATE = """
    current_setting('app.current_tenant_id', true) = 'BYPASS'
    OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)
"""


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def _tenant() -> sa.Column:
    return sa.Column("tenant_id", _uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)


def _enable_strict_rls(table: str) -> None:
    predicate = _STRICT_PREDICATE.format(table=table)
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table} ON {table}
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )


def upgrade() -> None:
    op.create_table(
        "business_teams",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        _tenant(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_by_user_id", _uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("department_id", _uuid(), sa.ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True),
        sa.CheckConstraint("status IN ('active','suspended','archived')", name="ck_business_teams_status"),
        sa.CheckConstraint("name <> ''", name="ck_business_teams_name"),
    )
    op.create_index("ix_business_teams_tenant_id", "business_teams", ["tenant_id"])
    op.create_index("ix_business_teams_created_by_user_id", "business_teams", ["created_by_user_id"])
    op.create_index("ix_business_teams_department_id", "business_teams", ["department_id"])

    op.create_table(
        "business_team_memberships",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        _tenant(),
        sa.Column(
            "business_team_id", _uuid(), sa.ForeignKey("business_teams.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("user_id", _uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("org_member_id", _uuid(), sa.ForeignKey("org_members.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('member','lead')", name="ck_business_team_memberships_role"),
        sa.CheckConstraint("status IN ('active','left','suspended')", name="ck_business_team_memberships_status"),
        sa.CheckConstraint(
            "user_id IS NOT NULL OR org_member_id IS NOT NULL",
            name="ck_business_team_memberships_person",
        ),
    )
    op.create_index("ix_business_team_memberships_tenant_id", "business_team_memberships", ["tenant_id"])
    op.create_index("ix_business_team_memberships_business_team_id", "business_team_memberships", ["business_team_id"])
    op.create_index("ix_business_team_memberships_user_id", "business_team_memberships", ["user_id"])
    op.create_index("ix_business_team_memberships_org_member_id", "business_team_memberships", ["org_member_id"])
    op.create_index(
        "uq_business_team_memberships_active_user",
        "business_team_memberships",
        ["business_team_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_business_team_memberships_active_org_member",
        "business_team_memberships",
        ["business_team_id", "org_member_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND org_member_id IS NOT NULL"),
    )

    op.create_table(
        "org_member_user_links",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        _tenant(),
        sa.Column("org_member_id", _uuid(), sa.ForeignKey("org_members.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("user_id", _uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column(
            "provider_id", _uuid(), sa.ForeignKey("identity_providers.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column("subject_kind", sa.String(32), nullable=True),
        sa.Column("match_subject", sa.String(255), nullable=False),
        sa.Column(
            "external_identity_id",
            _uuid(),
            sa.ForeignKey("external_identities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("evidence_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_by_user_id", _uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.CheckConstraint(
            "status IN ('active','conflict','inactive_user','revoked')",
            name="ck_org_member_user_links_status",
        ),
        sa.CheckConstraint(
            "subject_kind IS NULL OR subject_kind IN ('provider_user_id','provider_open_id','provider_union_id')",
            name="ck_org_member_user_links_subject_kind",
        ),
        sa.CheckConstraint("match_subject <> ''", name="ck_org_member_user_links_match_subject"),
        sa.CheckConstraint(
            "status <> 'active' OR (provider_id IS NOT NULL AND subject_kind IS NOT NULL AND match_subject <> '')",
            name="ck_org_member_user_links_active_requires_subject",
        ),
        sa.CheckConstraint(_LINK_EVIDENCE_SQL, name="ck_org_member_user_links_evidence"),
    )
    op.create_index("ix_org_member_user_links_tenant_id", "org_member_user_links", ["tenant_id"])
    op.create_index("ix_org_member_user_links_org_member_id", "org_member_user_links", ["org_member_id"])
    op.create_index("ix_org_member_user_links_user_id", "org_member_user_links", ["user_id"])
    op.create_index("ix_org_member_user_links_provider_id", "org_member_user_links", ["provider_id"])
    op.create_index(
        "ix_org_member_user_links_external_identity_id", "org_member_user_links", ["external_identity_id"]
    )
    op.create_index("ix_org_member_user_links_created_by_user_id", "org_member_user_links", ["created_by_user_id"])
    op.create_index(
        "uq_org_member_user_links_active_subject",
        "org_member_user_links",
        ["tenant_id", "provider_id", "subject_kind", "match_subject"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_org_member_user_links_active_member_kind",
        "org_member_user_links",
        ["tenant_id", "org_member_id", "provider_id", "subject_kind"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "department_directory_links",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        _tenant(),
        sa.Column(
            "org_department_id", _uuid(), sa.ForeignKey("org_departments.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("department_id", _uuid(), sa.ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_department_id", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','conflict','revoked')",
            name="ck_department_directory_links_status",
        ),
        sa.CheckConstraint("provider = 'feishu'", name="ck_department_directory_links_provider"),
        sa.CheckConstraint(
            "provider_department_id <> ''",
            name="ck_department_directory_links_provider_department",
        ),
    )
    op.create_index("ix_department_directory_links_tenant_id", "department_directory_links", ["tenant_id"])
    op.create_index(
        "ix_department_directory_links_org_department_id", "department_directory_links", ["org_department_id"]
    )
    op.create_index("ix_department_directory_links_department_id", "department_directory_links", ["department_id"])
    op.create_index(
        "uq_department_directory_links_active_org_department",
        "department_directory_links",
        ["tenant_id", "org_department_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_department_directory_links_active_provider_department",
        "department_directory_links",
        ["tenant_id", "provider", "provider_department_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_department_directory_links_active_department",
        "department_directory_links",
        ["tenant_id", "department_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND department_id IS NOT NULL"),
    )

    op.create_table(
        "identity_cutover_journal",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        _tenant(),
        sa.Column("object_kind", sa.String(64), nullable=False),
        sa.Column("object_id", _uuid(), nullable=False),
        sa.Column("before_json", postgresql.JSONB(), nullable=True),
        sa.Column("after_json", postgresql.JSONB(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("object_kind <> ''", name="ck_identity_cutover_journal_object_kind"),
        sa.CheckConstraint(
            "jsonb_typeof(after_json) = 'object'",
            name="ck_identity_cutover_journal_after_json",
        ),
    )
    op.create_index("ix_identity_cutover_journal_tenant_id", "identity_cutover_journal", ["tenant_id"])
    op.create_index("ix_identity_cutover_journal_object_id", "identity_cutover_journal", ["object_id"])

    op.create_table(
        "content_input_receipts",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        _tenant(),
        sa.Column("carrier_kind", sa.String(32), nullable=False),
        sa.Column("carrier_ref", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "dependencies_json",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "carrier_kind IN ('team_memory','workspace_artifact')",
            name="ck_content_input_receipts_carrier_kind",
        ),
        sa.CheckConstraint("carrier_ref <> ''", name="ck_content_input_receipts_carrier_ref"),
        sa.CheckConstraint(
            "length(content_sha256) = 64 AND content_sha256 = lower(content_sha256) "
            "AND translate(content_sha256, '0123456789abcdef', '') = ''",
            name="ck_content_input_receipts_content_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(dependencies_json) = 'array'",
            name="ck_content_input_receipts_dependencies_array",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "carrier_kind",
            "carrier_ref",
            "revision",
            name="uq_content_input_receipts_revision",
        ),
    )
    op.create_index("ix_content_input_receipts_tenant_id", "content_input_receipts", ["tenant_id"])

    for table in IDENTITY_LINK_TABLES:
        _enable_strict_rls(table)

    op.add_column("chat_sessions", sa.Column("audience_kind", sa.String(32), nullable=True))
    op.add_column("chat_sessions", sa.Column("audience_ref", _uuid(), nullable=True))
    op.add_column("chat_sessions", sa.Column("group_installation_ref", sa.String(200), nullable=True))
    op.add_column("chat_sessions", sa.Column("group_subject_id", sa.String(200), nullable=True))
    op.create_check_constraint("ck_chat_sessions_audience", "chat_sessions", _SESSION_AUDIENCE_SQL)
    op.create_foreign_key(
        "fk_chat_sessions_audience_ref",
        "chat_sessions",
        "business_teams",
        ["audience_ref"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column("tasks", sa.Column("responsible_user_id", _uuid(), nullable=True))
    op.add_column("tasks", sa.Column("business_team_id", _uuid(), nullable=True))
    op.add_column("tasks", sa.Column("audience_kind", sa.String(32), nullable=True))
    op.create_check_constraint("ck_tasks_audience_kind", "tasks", _AUDIENCE_KIND_SQL)
    op.create_foreign_key(
        "fk_tasks_responsible_user_id",
        "tasks",
        "users",
        ["responsible_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_tasks_business_team_id",
        "tasks",
        "business_teams",
        ["business_team_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column("agents", sa.Column("home_business_team_id", _uuid(), nullable=True))
    op.create_foreign_key(
        "fk_agents_home_business_team_id",
        "agents",
        "business_teams",
        ["home_business_team_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column("workspace_resource_manifests", sa.Column("audience_kind", sa.String(32), nullable=True))
    op.add_column("workspace_resource_manifests", sa.Column("audience_ref", _uuid(), nullable=True))
    op.create_check_constraint(
        "ck_workspace_resource_manifest_audience_kind",
        "workspace_resource_manifests",
        _AUDIENCE_KIND_SQL,
    )

    from app.db_bootstrap import apply_identity_link_guards

    apply_identity_link_guards(op.get_bind())


def downgrade() -> None:
    op.drop_constraint(
        "ck_workspace_resource_manifest_audience_kind", "workspace_resource_manifests", type_="check"
    )
    op.drop_column("workspace_resource_manifests", "audience_ref")
    op.drop_column("workspace_resource_manifests", "audience_kind")

    op.drop_constraint("fk_agents_home_business_team_id", "agents", type_="foreignkey")
    op.drop_column("agents", "home_business_team_id")

    op.drop_constraint("fk_tasks_business_team_id", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_responsible_user_id", "tasks", type_="foreignkey")
    op.drop_constraint("ck_tasks_audience_kind", "tasks", type_="check")
    op.drop_column("tasks", "audience_kind")
    op.drop_column("tasks", "business_team_id")
    op.drop_column("tasks", "responsible_user_id")

    op.drop_constraint("fk_chat_sessions_audience_ref", "chat_sessions", type_="foreignkey")
    op.drop_constraint("ck_chat_sessions_audience", "chat_sessions", type_="check")
    op.drop_column("chat_sessions", "group_subject_id")
    op.drop_column("chat_sessions", "group_installation_ref")
    op.drop_column("chat_sessions", "audience_ref")
    op.drop_column("chat_sessions", "audience_kind")

    op.execute("DROP TRIGGER IF EXISTS trg_business_teams_creator_immutable ON business_teams")
    op.execute("DROP TRIGGER IF EXISTS trg_content_input_receipts_dependencies ON content_input_receipts")
    op.execute("DROP TRIGGER IF EXISTS trg_identity_cutover_journal_immutable ON identity_cutover_journal")
    op.execute("DROP TRIGGER IF EXISTS trg_identity_cutover_journal_no_truncate ON identity_cutover_journal")
    op.drop_table("content_input_receipts")
    op.drop_table("identity_cutover_journal")
    op.drop_table("department_directory_links")
    op.drop_table("org_member_user_links")
    op.drop_table("business_team_memberships")
    op.drop_table("business_teams")
    op.execute("DROP FUNCTION IF EXISTS reject_business_team_creator_change()")
    op.execute("DROP FUNCTION IF EXISTS reject_invalid_content_input_receipt()")
    op.execute("DROP FUNCTION IF EXISTS reject_identity_cutover_journal_mutation()")
