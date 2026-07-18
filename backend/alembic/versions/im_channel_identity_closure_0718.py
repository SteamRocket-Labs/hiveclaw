"""Close IM self-identity binding and external Session command authority.

Revision ID: im_channel_identity_closure_0718
Revises: collaboration_runtime_closure_0717
Create Date: 2026-07-18
"""

from __future__ import annotations

from alembic import op


revision = "im_channel_identity_closure_0718"
down_revision = "collaboration_runtime_closure_0717"
branch_labels = None
depends_on = None


def build_verified_wechat_identity_backfill_sql() -> str:
    """Backfill only bindings already proven by the canonical principal ledger.

    Legacy ChannelConfig rows did not retain the authenticated installer.  It is
    unsafe to guess that the Agent owner scanned the QR code, so this migration
    deliberately refuses owner/creator inference.  A row is verified only when
    exactly one active principal for this concrete installation already links
    the configured iLink subject to one active same-tenant User.
    """

    return """
        WITH verified AS (
            SELECT
                cc.id AS channel_config_id,
                (array_agg(DISTINCT p.linked_user_id))[1] AS user_id,
                MAX(COALESCE(p.linked_at, p.updated_at, p.created_at)) AS verified_at
            FROM channel_configs AS cc
            JOIN external_principals AS p
              ON p.channel_config_id = cc.id
             AND p.tenant_id = cc.tenant_id
             AND p.provider = 'wechat_personal'
             AND p.status = 'active'
             AND p.linked_user_id IS NOT NULL
             AND p.subject_id = NULLIF(cc.extra_config ->> 'ilink_user_id', '')
            JOIN users AS u
              ON u.id = p.linked_user_id
             AND u.tenant_id = cc.tenant_id
             AND u.is_active IS TRUE
            WHERE cc.channel_type = 'wechat_personal'
            GROUP BY cc.id
            HAVING COUNT(DISTINCT p.linked_user_id) = 1
        )
        UPDATE channel_configs AS cc
        SET self_identity_user_id = verified.user_id,
            self_identity_verified_at = verified.verified_at,
            updated_at = NOW()
        FROM verified
        WHERE cc.id = verified.channel_config_id
          AND (
            cc.self_identity_user_id IS DISTINCT FROM verified.user_id
            OR cc.self_identity_verified_at IS NULL
          )
    """


def upgrade() -> None:
    op.execute(
        "ALTER TABLE channel_configs "
        "ADD COLUMN IF NOT EXISTS self_identity_user_id uuid NULL"
    )
    op.execute(
        "ALTER TABLE channel_configs "
        "ADD COLUMN IF NOT EXISTS self_identity_verified_at timestamptz NULL"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_channel_configs_self_identity_user_id_users'
          ) THEN
            ALTER TABLE channel_configs
            ADD CONSTRAINT fk_channel_configs_self_identity_user_id_users
            FOREIGN KEY (self_identity_user_id) REFERENCES users(id) ON DELETE SET NULL;
          END IF;
        END
        $migration$
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_channel_configs_self_identity_user_id "
        "ON channel_configs (self_identity_user_id)"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_channel_configs_self_identity_channel'
          ) THEN
            ALTER TABLE channel_configs
            ADD CONSTRAINT ck_channel_configs_self_identity_channel
            CHECK (
              self_identity_user_id IS NULL
              OR (
                channel_type = 'wechat_personal'
                AND self_identity_verified_at IS NOT NULL
              )
            );
          END IF;
        END
        $migration$
        """
    )

    op.execute(
        "ALTER TABLE session_commands "
        "ADD COLUMN IF NOT EXISTS principal_type varchar(32) NOT NULL DEFAULT 'user'"
    )
    op.execute("UPDATE session_commands SET principal_type = 'user' WHERE principal_type IS NULL")
    op.execute("ALTER TABLE session_commands DROP CONSTRAINT IF EXISTS uq_session_commands_idempotency")
    op.execute(
        """
        UPDATE session_commands AS sc
        SET principal_type = 'external_principal',
            principal_id = cs.external_principal_id,
            updated_at = NOW()
        FROM chat_sessions AS cs
        JOIN external_principals AS ep
          ON ep.id = cs.external_principal_id
        WHERE sc.session_id = cs.id
          AND sc.tenant_id = cs.tenant_id
          AND ep.tenant_id = sc.tenant_id
          AND sc.namespace = 'human_input'
          AND sc.principal_type = 'user'
          AND cs.user_id IS NOT NULL
          AND sc.principal_id = cs.user_id
          AND (
            (ep.provider = 'teams' AND cs.source_channel = 'microsoft_teams')
            OR (
              ep.provider = cs.source_channel
              AND ep.provider IN (
                'discord', 'dingtalk', 'slack', 'telegram',
                'wechat_personal', 'wecom'
              )
            )
          )
        """
    )
    op.execute("ALTER TABLE session_commands DROP CONSTRAINT IF EXISTS ck_session_commands_principal_type")
    op.execute(
        "ALTER TABLE session_commands ADD CONSTRAINT ck_session_commands_principal_type "
        "CHECK (principal_type IN ('user','external_principal'))"
    )
    op.execute(
        "ALTER TABLE session_commands ADD CONSTRAINT uq_session_commands_idempotency UNIQUE "
        "(tenant_id, principal_type, principal_id, session_id, namespace, idempotency_key)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_commands_principal_type_id "
        "ON session_commands (principal_type, principal_id)"
    )

    op.execute(build_verified_wechat_identity_backfill_sql())


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_session_commands_principal_type_id")
    op.execute("ALTER TABLE session_commands DROP CONSTRAINT IF EXISTS uq_session_commands_idempotency")
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM session_commands AS sc
            JOIN chat_sessions AS cs ON cs.id = sc.session_id
            WHERE sc.principal_type = 'external_principal'
              AND cs.user_id IS NULL
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade IM identity authority while unbound external-principal commands exist';
          END IF;
        END
        $migration$
        """
    )
    op.execute(
        """
        UPDATE session_commands AS sc
        SET principal_type = 'user',
            principal_id = cs.user_id,
            updated_at = NOW()
        FROM chat_sessions AS cs
        WHERE sc.session_id = cs.id
          AND sc.principal_type = 'external_principal'
          AND cs.user_id IS NOT NULL
        """
    )
    op.execute(
        "ALTER TABLE session_commands ADD CONSTRAINT uq_session_commands_idempotency UNIQUE "
        "(tenant_id, principal_id, session_id, namespace, idempotency_key)"
    )
    op.execute("ALTER TABLE session_commands DROP CONSTRAINT IF EXISTS ck_session_commands_principal_type")
    op.execute("ALTER TABLE session_commands DROP COLUMN IF EXISTS principal_type")
    op.execute("ALTER TABLE channel_configs DROP CONSTRAINT IF EXISTS ck_channel_configs_self_identity_channel")
    op.execute("DROP INDEX IF EXISTS ix_channel_configs_self_identity_user_id")
    op.execute(
        "ALTER TABLE channel_configs "
        "DROP CONSTRAINT IF EXISTS fk_channel_configs_self_identity_user_id_users"
    )
    op.execute("ALTER TABLE channel_configs DROP COLUMN IF EXISTS self_identity_verified_at")
    op.execute("ALTER TABLE channel_configs DROP COLUMN IF EXISTS self_identity_user_id")
