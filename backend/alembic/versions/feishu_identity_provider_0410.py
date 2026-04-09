"""Add provider-driven Feishu identity tables and org member linkage.

Revision ID: feishu_identity_provider_0410
Revises: heartbeat_interval_45min_0408
Create Date: 2026-04-10
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "feishu_identity_provider_0410"
down_revision: Union[str, None] = "heartbeat_interval_45min_0408"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def _utcnow():
    return datetime.now(timezone.utc)


def upgrade() -> None:
    op.create_table(
        "identity_providers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("provider_type", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sso_login_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_identity_providers_provider_type", "identity_providers", ["provider_type"])
    op.create_index("ix_identity_providers_tenant_id", "identity_providers", ["tenant_id"])

    op.create_table(
        "external_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "provider_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("identity_providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_user_id", sa.String(length=255), nullable=True),
        sa.Column("provider_open_id", sa.String(length=255), nullable=True),
        sa.Column("provider_union_id", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("mobile", sa.String(length=64), nullable=True),
        sa.Column("raw_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider_id", "provider_user_id", name="uq_external_identities_provider_user_id"),
        sa.UniqueConstraint("provider_id", "provider_open_id", name="uq_external_identities_provider_open_id"),
        sa.UniqueConstraint("provider_id", "provider_union_id", name="uq_external_identities_provider_union_id"),
    )
    op.create_index("ix_external_identities_provider_id", "external_identities", ["provider_id"])
    op.create_index("ix_external_identities_user_id", "external_identities", ["user_id"])
    op.create_index("ix_external_identities_provider_user_id", "external_identities", ["provider_user_id"])
    op.create_index("ix_external_identities_provider_open_id", "external_identities", ["provider_open_id"])
    op.create_index("ix_external_identities_provider_union_id", "external_identities", ["provider_union_id"])

    op.create_table(
        "sso_scan_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("provider_type", sa.String(length=50), nullable=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sso_scan_sessions_tenant_id", "sso_scan_sessions", ["tenant_id"])
    op.create_index("ix_sso_scan_sessions_user_id", "sso_scan_sessions", ["user_id"])

    op.add_column("org_members", sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("org_members", sa.Column("external_id", sa.String(length=100), nullable=True))
    op.add_column("org_members", sa.Column("open_id", sa.String(length=100), nullable=True))
    op.add_column("org_members", sa.Column("unionid", sa.String(length=100), nullable=True))
    op.create_foreign_key(
        "fk_org_members_provider_id_identity_providers",
        "org_members",
        "identity_providers",
        ["provider_id"],
        ["id"],
    )
    op.create_index("ix_org_members_external_id", "org_members", ["external_id"])
    op.create_index("ix_org_members_open_id", "org_members", ["open_id"])
    op.create_index("ix_org_members_unionid", "org_members", ["unionid"])

    conn = op.get_bind()
    providers = sa.table(
        "identity_providers",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("provider_type", sa.String()),
        sa.column("name", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("sso_login_enabled", sa.Boolean()),
        sa.column("config", postgresql.JSONB(astext_type=sa.Text())),
        sa.column("tenant_id", postgresql.UUID(as_uuid=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    tenant_settings = sa.table(
        "tenant_settings",
        sa.column("tenant_id", postgresql.UUID(as_uuid=True)),
        sa.column("key", sa.String()),
        sa.column("value", postgresql.JSONB(astext_type=sa.Text())),
    )
    users = sa.table(
        "users",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("tenant_id", postgresql.UUID(as_uuid=True)),
        sa.column("email", sa.String()),
        sa.column("feishu_user_id", sa.String()),
        sa.column("feishu_open_id", sa.String()),
        sa.column("feishu_union_id", sa.String()),
    )
    org_members = sa.table(
        "org_members",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("tenant_id", postgresql.UUID(as_uuid=True)),
        sa.column("feishu_user_id", sa.String()),
        sa.column("feishu_open_id", sa.String()),
        sa.column("provider_id", postgresql.UUID(as_uuid=True)),
        sa.column("external_id", sa.String()),
        sa.column("open_id", sa.String()),
        sa.column("unionid", sa.String()),
    )
    external_identities = sa.table(
        "external_identities",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("provider_id", postgresql.UUID(as_uuid=True)),
        sa.column("user_id", postgresql.UUID(as_uuid=True)),
        sa.column("provider_user_id", sa.String()),
        sa.column("provider_open_id", sa.String()),
        sa.column("provider_union_id", sa.String()),
        sa.column("email", sa.String()),
        sa.column("mobile", sa.String()),
        sa.column("raw_profile", postgresql.JSONB(astext_type=sa.Text())),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    provider_by_tenant: dict[uuid.UUID | None, uuid.UUID] = {}
    now = _utcnow()

    provider_rows = conn.execute(
        sa.select(tenant_settings.c.tenant_id, tenant_settings.c.value).where(tenant_settings.c.key == "feishu_org_sync")
    ).mappings()
    for row in provider_rows:
        cfg = row["value"] or {}
        if not cfg.get("app_id") or not cfg.get("app_secret"):
            continue
        provider_id = uuid.uuid4()
        conn.execute(
            sa.insert(providers).values(
                id=provider_id,
                provider_type="feishu",
                name="Feishu Tenant Provider",
                is_active=True,
                sso_login_enabled=True,
                config=cfg,
                tenant_id=row["tenant_id"],
                created_at=now,
                updated_at=now,
            )
        )
        provider_by_tenant[row["tenant_id"]] = provider_id

    legacy_tenants: set[uuid.UUID | None] = set()
    legacy_user_tenants = conn.execute(
        sa.select(users.c.tenant_id).where(
            sa.or_(
                users.c.feishu_user_id.is_not(None),
                users.c.feishu_open_id.is_not(None),
                users.c.feishu_union_id.is_not(None),
            )
        )
    )
    legacy_member_tenants = conn.execute(
        sa.select(org_members.c.tenant_id).where(
            sa.or_(
                org_members.c.feishu_user_id.is_not(None),
                org_members.c.feishu_open_id.is_not(None),
            )
        )
    )
    legacy_tenants.update(row[0] for row in legacy_user_tenants)
    legacy_tenants.update(row[0] for row in legacy_member_tenants)

    for tenant_id in legacy_tenants:
        if tenant_id in provider_by_tenant:
            continue
        provider_id = uuid.uuid4()
        conn.execute(
            sa.insert(providers).values(
                id=provider_id,
                provider_type="feishu",
                name="Feishu Legacy Provider" if tenant_id is not None else "Feishu Global Provider",
                is_active=True,
                sso_login_enabled=tenant_id is None,
                config={},
                tenant_id=tenant_id,
                created_at=now,
                updated_at=now,
            )
        )
        provider_by_tenant[tenant_id] = provider_id

    seen_identifiers: set[tuple[uuid.UUID, str, str]] = set()
    user_rows = conn.execute(
        sa.select(
            users.c.id,
            users.c.tenant_id,
            users.c.email,
            users.c.feishu_user_id,
            users.c.feishu_open_id,
            users.c.feishu_union_id,
        ).where(
            sa.or_(
                users.c.feishu_user_id.is_not(None),
                users.c.feishu_open_id.is_not(None),
                users.c.feishu_union_id.is_not(None),
            )
        )
    ).mappings()
    for row in user_rows:
        provider_id = provider_by_tenant.get(row["tenant_id"])
        if provider_id is None:
            continue
        identifiers = [
            ("user_id", row["feishu_user_id"]),
            ("open_id", row["feishu_open_id"]),
            ("union_id", row["feishu_union_id"]),
        ]
        if any(value and (provider_id, key, value) in seen_identifiers for key, value in identifiers):
            continue
        for key, value in identifiers:
            if value:
                seen_identifiers.add((provider_id, key, value))
        conn.execute(
            sa.insert(external_identities).values(
                id=uuid.uuid4(),
                provider_id=provider_id,
                user_id=row["id"],
                provider_user_id=row["feishu_user_id"],
                provider_open_id=row["feishu_open_id"],
                provider_union_id=row["feishu_union_id"],
                email=row["email"],
                mobile=None,
                raw_profile={"backfilled_from": "users"},
                created_at=now,
                updated_at=now,
            )
        )

    member_rows = conn.execute(
        sa.select(org_members.c.id, org_members.c.tenant_id, org_members.c.feishu_user_id, org_members.c.feishu_open_id)
    ).mappings()
    for row in member_rows:
        provider_id = provider_by_tenant.get(row["tenant_id"])
        if provider_id is None:
            continue
        conn.execute(
            sa.update(org_members)
            .where(org_members.c.id == row["id"])
            .values(
                provider_id=provider_id,
                external_id=row["feishu_user_id"],
                open_id=row["feishu_open_id"],
            )
        )


def downgrade() -> None:
    op.drop_index("ix_org_members_unionid", table_name="org_members")
    op.drop_index("ix_org_members_open_id", table_name="org_members")
    op.drop_index("ix_org_members_external_id", table_name="org_members")
    op.drop_constraint("fk_org_members_provider_id_identity_providers", "org_members", type_="foreignkey")
    op.drop_column("org_members", "unionid")
    op.drop_column("org_members", "open_id")
    op.drop_column("org_members", "external_id")
    op.drop_column("org_members", "provider_id")

    op.drop_index("ix_sso_scan_sessions_user_id", table_name="sso_scan_sessions")
    op.drop_index("ix_sso_scan_sessions_tenant_id", table_name="sso_scan_sessions")
    op.drop_table("sso_scan_sessions")

    op.drop_index("ix_external_identities_provider_union_id", table_name="external_identities")
    op.drop_index("ix_external_identities_provider_open_id", table_name="external_identities")
    op.drop_index("ix_external_identities_provider_user_id", table_name="external_identities")
    op.drop_index("ix_external_identities_user_id", table_name="external_identities")
    op.drop_index("ix_external_identities_provider_id", table_name="external_identities")
    op.drop_table("external_identities")

    op.drop_index("ix_identity_providers_tenant_id", table_name="identity_providers")
    op.drop_index("ix_identity_providers_provider_type", table_name="identity_providers")
    op.drop_table("identity_providers")
