"""Add installation-scoped external principals and retire synthetic channel users.

Revision ID: external_principals_0711
Revises: channel_ingress_inbox_0711
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "external_principals_0711"
down_revision = "channel_ingress_inbox_0711"
branch_labels = None
depends_on = None

_EXTERNAL_PRINCIPAL_RLS_TABLES = (
    "external_principals",
    "external_principal_binding_events",
)


def _uuid_column(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "external_principals",
        _uuid_column("id", nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("installation_ref", sa.String(length=200), nullable=False),
        sa.Column(
            "channel_config_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channel_configs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("subject_id", sa.String(length=512), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("profile_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "linked_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "installation_ref",
            "subject_id",
            name="uq_external_principals_tenant_provider_installation_subject",
        ),
        sa.CheckConstraint("status IN ('active','revoked')", name="ck_external_principals_status"),
    )
    for name, columns in (
        ("ix_external_principals_tenant_id", ["tenant_id"]),
        ("ix_external_principals_provider", ["provider"]),
        ("ix_external_principals_channel_config_id", ["channel_config_id"]),
        ("ix_external_principals_linked_user_id", ["linked_user_id"]),
        ("ix_external_principals_status", ["status"]),
        ("ix_external_principals_tenant_provider_status", ["tenant_id", "provider", "status"]),
    ):
        op.create_index(name, "external_principals", columns)

    op.create_table(
        "external_principal_binding_events",
        _uuid_column("id", nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "external_principal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("external_principals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "previous_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "new_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "action IN ('linked','unlinked','revoked','reactivated')",
            name="ck_external_principal_binding_events_action",
        ),
    )
    for name, columns in (
        ("ix_external_principal_binding_events_tenant_id", ["tenant_id"]),
        ("ix_external_principal_binding_events_external_principal_id", ["external_principal_id"]),
        ("ix_external_principal_binding_events_actor_user_id", ["actor_user_id"]),
        (
            "ix_external_principal_binding_events_principal_created",
            ["external_principal_id", "created_at"],
        ),
    ):
        op.create_index(name, "external_principal_binding_events", columns)

    for table, column in (
        ("chat_sessions", "external_principal_id"),
        ("chat_messages", "external_principal_id"),
        ("audit_logs", "external_principal_id"),
        ("approval_requests", "requested_by_external_principal_id"),
        ("runtime_budget_runs", "root_external_principal_id"),
        ("channel_ingress_events", "external_principal_id"),
    ):
        op.add_column(table, _uuid_column(column))
        op.create_foreign_key(
            f"fk_{table}_{column}",
            table,
            "external_principals",
            [column],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(f"ix_{table}_{column}", table, [column])

    op.alter_column(
        "chat_sessions",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.alter_column(
        "chat_messages",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )

    # Existing tenant tables already FORCE RLS. The release migration is a
    # fleet-wide trusted backfill, so it must explicitly enter the same audited
    # BYPASS sentinel used by owner-only maintenance paths; otherwise the
    # SELECT below silently sees zero legacy rows.
    # Use session scope rather than SET LOCAL because Alembic/SQLAlchemy may
    # open implicit transaction boundaries around DDL on upgrade paths.
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', false)")

    # Backfill fact table: tenant_id, provider, installation_ref, subject_id.
    # A temporary relation keeps the exact same derivation across principal,
    # session, message, runtime, approval, and audit projections.
    op.execute(
        """
        CREATE TEMP TABLE external_principal_backfill ON COMMIT DROP AS
        SELECT
            s.id AS session_id,
            s.tenant_id,
            s.agent_id,
            s.user_id AS legacy_user_id,
            CASE
                WHEN s.source_channel = 'microsoft_teams' THEN 'teams'
                WHEN s.source_channel = 'wechat_personal' THEN 'wechat_personal'
                ELSE s.source_channel
            END AS provider,
            COALESCE(cc.id::text, 'legacy:' || s.source_channel || ':' || s.agent_id::text) AS installation_ref,
            cc.id AS channel_config_id,
            COALESCE(
                NULLIF(s.delivery_target_json->>'sender_id', ''),
                NULLIF(s.delivery_target_json->>'sender_staff_id', ''),
                NULLIF(s.delivery_target_json->>'from_user', ''),
                NULLIF(s.delivery_target_json->>'to_user_id', ''),
                regexp_replace(
                    u.username,
                    '^(slack_|telegram_|tg_|discord_|teams_|wecom_|wechat_|dingtalk_)',
                    ''
                )
            ) AS subject_id,
            u.display_name,
            u.is_active AS legacy_user_was_active,
            COALESCE(s.last_message_at, s.created_at, now()) AS last_seen_at
        FROM chat_sessions s
        JOIN users u ON u.id = s.user_id
        LEFT JOIN LATERAL (
            SELECT channel_configs.id
            FROM channel_configs
            WHERE channel_configs.agent_id = s.agent_id
              AND channel_configs.channel_type::text IN (
                  s.source_channel,
                  CASE WHEN s.source_channel = 'teams' THEN 'microsoft_teams' ELSE s.source_channel END
              )
            ORDER BY channel_configs.created_at DESC NULLS LAST, channel_configs.id
            LIMIT 1
        ) cc ON true
        WHERE
            u.email LIKE '%@slack.local'
            OR u.email LIKE '%@telegram.local'
            OR u.email LIKE '%@discord.local'
            OR u.email LIKE '%@teams.local'
            OR u.email LIKE '%@wecom.local'
            OR u.email LIKE '%@wechat.local'
            OR u.email LIKE '%@dingtalk.local'
        """
    )
    op.execute(
        """
        INSERT INTO external_principals (
            id, tenant_id, provider, installation_ref, channel_config_id,
            subject_id, display_name, profile_json, status,
            first_seen_at, last_seen_at, created_at, updated_at
        )
        SELECT DISTINCT ON (tenant_id, provider, installation_ref, subject_id)
            md5(tenant_id::text || '|' || provider || '|' || installation_ref || '|' || subject_id)::uuid,
            tenant_id,
            provider,
            installation_ref,
            channel_config_id,
            subject_id,
            COALESCE(NULLIF(display_name, ''), 'External user'),
            jsonb_build_object(
                'legacy_synthetic_user_id', legacy_user_id::text,
                'legacy_synthetic_user_was_active', legacy_user_was_active,
                'backfilled', true
            ),
            'active',
            last_seen_at,
            last_seen_at,
            now(),
            now()
        FROM external_principal_backfill
        WHERE tenant_id IS NOT NULL AND provider IS NOT NULL AND subject_id IS NOT NULL AND subject_id <> ''
        ORDER BY tenant_id, provider, installation_ref, subject_id, last_seen_at DESC
        ON CONFLICT (tenant_id, provider, installation_ref, subject_id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE chat_sessions s
        SET external_principal_id = ep.id, user_id = NULL
        FROM external_principal_backfill b
        JOIN external_principals ep
          ON ep.tenant_id = b.tenant_id
         AND ep.provider = b.provider
         AND ep.installation_ref = b.installation_ref
         AND ep.subject_id = b.subject_id
        WHERE s.id = b.session_id
        """
    )
    op.execute(
        """
        UPDATE chat_messages m
        SET external_principal_id = s.external_principal_id, user_id = NULL
        FROM chat_sessions s
        WHERE m.conversation_id = s.id::text
          AND s.external_principal_id IS NOT NULL
          AND m.user_id IN (SELECT legacy_user_id FROM external_principal_backfill)
        """
    )
    op.execute(
        """
        UPDATE channel_ingress_events e
        SET external_principal_id = s.external_principal_id
        FROM chat_sessions s
        WHERE e.result_session_id = s.id AND s.external_principal_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE runtime_budget_runs b
        SET root_external_principal_id = s.external_principal_id, root_user_id = NULL
        FROM chat_sessions s
        WHERE b.root_session_id = s.id::text
          AND s.external_principal_id IS NOT NULL
          AND b.root_user_id IN (SELECT legacy_user_id FROM external_principal_backfill)
        """
    )
    op.execute(
        """
        UPDATE audit_logs a
        SET external_principal_id = p.external_principal_id, user_id = NULL
        FROM (
            SELECT DISTINCT ON (legacy_user_id, tenant_id)
                legacy_user_id, tenant_id,
                md5(tenant_id::text || '|' || provider || '|' || installation_ref || '|' || subject_id)::uuid
                    AS external_principal_id
            FROM external_principal_backfill
            ORDER BY legacy_user_id, tenant_id, last_seen_at DESC
        ) p
        WHERE a.user_id = p.legacy_user_id AND a.tenant_id = p.tenant_id
        """
    )
    op.execute(
        """
        UPDATE approval_requests a
        SET
            requested_by_external_principal_id = p.external_principal_id,
            requested_by = NULL,
            details = (
                COALESCE(a.details::jsonb, '{}'::jsonb)
                || jsonb_build_object(
                    'legacy_external_identity_previous_status', a.status::text,
                    'legacy_external_identity_previous_execution_status', a.execution_status,
                    'legacy_external_identity_previous_requested_by', a.requested_by::text,
                    'legacy_external_identity_previous_resolved_at', a.resolved_at,
                    'legacy_external_identity_reconciliation', 'requires_explicit_user_binding_and_new_approval'
                )
            )::json,
            status = CASE
                WHEN a.status IN ('pending'::approval_status_enum, 'approved'::approval_status_enum)
                    THEN 'rejected'::approval_status_enum
                ELSE a.status
            END,
            execution_status = CASE
                WHEN a.status IN ('pending'::approval_status_enum, 'approved'::approval_status_enum)
                    THEN 'needs_reapproval'
                ELSE a.execution_status
            END,
            resolved_at = CASE
                WHEN a.status IN ('pending'::approval_status_enum, 'approved'::approval_status_enum)
                    THEN COALESCE(a.resolved_at, now())
                ELSE a.resolved_at
            END
        FROM (
            SELECT DISTINCT ON (legacy_user_id, tenant_id)
                legacy_user_id, tenant_id,
                md5(tenant_id::text || '|' || provider || '|' || installation_ref || '|' || subject_id)::uuid
                    AS external_principal_id
            FROM external_principal_backfill
            ORDER BY legacy_user_id, tenant_id, last_seen_at DESC
        ) p
        WHERE a.requested_by = p.legacy_user_id AND a.tenant_id = p.tenant_id
        """
    )
    op.execute(
        """
        UPDATE runtime_tasks r
        SET
            status = CASE
                WHEN r.status IN ('pending','running') THEN 'needs_reconciliation'
                ELSE r.status
            END,
            metadata_json = ((
                COALESCE(r.metadata_json::jsonb, '{}'::jsonb)
                - 'user_id'
            ) || jsonb_build_object(
                'external_principal_id', s.external_principal_id::text,
                'external_authority_bound', false,
                'legacy_external_identity_reconciled', true
            ))::json
        FROM chat_sessions s
        WHERE r.parent_session_id = s.id::text AND s.external_principal_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE users
        SET is_active = false
        WHERE
            email LIKE '%@slack.local'
            OR email LIKE '%@telegram.local'
            OR email LIKE '%@discord.local'
            OR email LIKE '%@teams.local'
            OR email LIKE '%@wecom.local'
            OR email LIKE '%@wechat.local'
            OR email LIKE '%@dingtalk.local'
        """
    )

    op.execute("SELECT set_config('app.current_tenant_id', '', false)")

    for table in _EXTERNAL_PRINCIPAL_RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation_{table} ON {table}
            USING (
                current_setting('app.current_tenant_id', true) = 'BYPASS'
                OR tenant_id::text = current_setting('app.current_tenant_id', true)
            )
            WITH CHECK (
                current_setting('app.current_tenant_id', true) = 'BYPASS'
                OR tenant_id::text = current_setting('app.current_tenant_id', true)
            )
            """
        )

    # Migration-level safety net. Fresh bootstrap is covered by application
    # deletion paths; upgraded deployments also fail closed if an old caller
    # deletes ChannelConfig directly.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION revoke_external_principals_on_channel_config_delete()
        RETURNS trigger AS $$
        BEGIN
            INSERT INTO external_principal_binding_events (
                id, tenant_id, external_principal_id, action,
                previous_user_id, new_user_id, reason, metadata_json, created_at
            )
            SELECT
                md5(p.id::text || clock_timestamp()::text || random()::text)::uuid,
                p.tenant_id,
                p.id,
                'revoked',
                p.linked_user_id,
                NULL,
                'channel config deleted',
                jsonb_build_object('channel_config_id', OLD.id::text),
                now()
            FROM external_principals p
            WHERE p.channel_config_id = OLD.id AND p.status = 'active';

            UPDATE external_principals
            SET status = 'revoked', revoked_at = now(), linked_user_id = NULL, linked_at = NULL
            WHERE channel_config_id = OLD.id AND status = 'active';
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_revoke_external_principals_on_channel_config_delete ON channel_configs")
    op.execute(
        """
        CREATE TRIGGER trg_revoke_external_principals_on_channel_config_delete
        BEFORE DELETE ON channel_configs
        FOR EACH ROW EXECUTE FUNCTION revoke_external_principals_on_channel_config_delete()
        """
    )


def downgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', false)")
    op.execute("DROP TRIGGER IF EXISTS trg_revoke_external_principals_on_channel_config_delete ON channel_configs")
    op.execute("DROP FUNCTION IF EXISTS revoke_external_principals_on_channel_config_delete()")

    op.execute(
        """
        UPDATE approval_requests
        SET
            requested_by = NULLIF(details::jsonb->>'legacy_external_identity_previous_requested_by', '')::uuid,
            status = (details::jsonb->>'legacy_external_identity_previous_status')::approval_status_enum,
            execution_status = details::jsonb->>'legacy_external_identity_previous_execution_status',
            resolved_at = NULLIF(
                details::jsonb->>'legacy_external_identity_previous_resolved_at',
                ''
            )::timestamptz,
            details = (
                details::jsonb
                - 'legacy_external_identity_previous_status'
                - 'legacy_external_identity_previous_execution_status'
                - 'legacy_external_identity_previous_requested_by'
                - 'legacy_external_identity_previous_resolved_at'
                - 'legacy_external_identity_reconciliation'
            )::json
        WHERE details::jsonb ? 'legacy_external_identity_reconciliation'
        """
    )
    op.execute(
        """
        UPDATE users u
        SET is_active = COALESCE(
            NULLIF(p.profile_json->>'legacy_synthetic_user_was_active', '')::boolean,
            u.is_active
        )
        FROM external_principals p
        WHERE NULLIF(p.profile_json->>'legacy_synthetic_user_id', '')::uuid = u.id
        """
    )

    # Reconstruct inactive compatibility users so the old non-null User FKs can
    # represent external history without assigning it to an owner/admin.
    op.execute(
        """
        INSERT INTO users (
            id, username, email, password_hash, display_name, role, tenant_id,
            is_active, must_change_password, tokens_used_today, tokens_used_month,
            tokens_used_total, created_at, updated_at
        )
        SELECT
            md5('external-principal-downgrade|' || p.id::text)::uuid,
            'legacy_ext_' || replace(p.id::text, '-', ''),
            replace(p.id::text, '-', '') || '@external-downgrade.local',
            '!',
            p.display_name,
            'member'::user_role_enum,
            p.tenant_id,
            false,
            false,
            0,
            0,
            0,
            now(),
            now()
        FROM external_principals p
        WHERE NULLIF(p.profile_json->>'legacy_synthetic_user_id', '') IS NULL
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE chat_sessions s
        SET user_id = COALESCE(
            p.linked_user_id,
            NULLIF(p.profile_json->>'legacy_synthetic_user_id', '')::uuid,
            md5('external-principal-downgrade|' || p.id::text)::uuid
        )
        FROM external_principals p
        WHERE s.external_principal_id = p.id AND s.user_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE chat_messages m
        SET user_id = COALESCE(
            p.linked_user_id,
            NULLIF(p.profile_json->>'legacy_synthetic_user_id', '')::uuid,
            md5('external-principal-downgrade|' || p.id::text)::uuid
        )
        FROM external_principals p
        WHERE m.external_principal_id = p.id AND m.user_id IS NULL
        """
    )
    op.alter_column(
        "chat_messages",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "chat_sessions",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    for table, column in reversed(
        (
            ("chat_sessions", "external_principal_id"),
            ("chat_messages", "external_principal_id"),
            ("audit_logs", "external_principal_id"),
            ("approval_requests", "requested_by_external_principal_id"),
            ("runtime_budget_runs", "root_external_principal_id"),
            ("channel_ingress_events", "external_principal_id"),
        )
    ):
        op.drop_index(f"ix_{table}_{column}", table_name=table)
        # Bootstrap/create_all may let PostgreSQL choose the FK name, while the
        # release upgrade uses our explicit name. Dropping the column removes
        # either form; remove the explicit migration name when present.
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "fk_{table}_{column}"')
        op.drop_column(table, column)

    op.drop_table("external_principal_binding_events")
    op.drop_table("external_principals")
    op.execute("SELECT set_config('app.current_tenant_id', '', false)")
