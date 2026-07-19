"""Make provider proof authoritative for IM-to-User identity binding.

Revision ID: im_user_verified_binding_0719
Revises: im_channel_identity_closure_0718
Create Date: 2026-07-19
"""

from __future__ import annotations

from alembic import op


revision = "im_user_verified_binding_0719"
down_revision = "im_channel_identity_closure_0718"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE external_principals ADD COLUMN IF NOT EXISTS binding_method varchar(40) NULL")
    op.execute(
        "ALTER TABLE external_principals ADD COLUMN IF NOT EXISTS binding_verified_at timestamptz NULL"
    )

    # Preserve only legacy WeChat bindings whose principal was created by the
    # authenticated QR-connect callback and whose channel projection records
    # the same scanner. The previous migration could derive the projection from
    # an older principal assignment, so the projection alone is not proof.
    # No owner/creator/email inference is permitted. New Feishu/Lark proof is
    # written atomically by the QR flow.
    op.execute(
        """
        UPDATE external_principals AS ep
        SET binding_method = 'wechat_qr',
            binding_verified_at = COALESCE(
                cc.self_identity_verified_at,
                ep.linked_at,
                ep.updated_at,
                ep.created_at
            )
        FROM channel_configs AS cc
        WHERE ep.channel_config_id = cc.id
          AND ep.tenant_id = cc.tenant_id
          AND ep.provider = 'wechat_personal'
          AND ep.status = 'active'
          AND ep.linked_user_id IS NOT NULL
          AND cc.channel_type = 'wechat_personal'
          AND cc.self_identity_user_id = ep.linked_user_id
          AND cc.self_identity_verified_at IS NOT NULL
          AND ep.subject_id = NULLIF(cc.extra_config ->> 'ilink_user_id', '')
          AND ep.profile_json ->> 'identity_source' = 'authenticated_channel_connect'
        """
    )

    # Record and remove every unproven legacy assignment. Admin-selected rows
    # cannot establish identity; their historical event remains auditable.
    op.execute(
        """
        INSERT INTO external_principal_binding_events (
            id, tenant_id, external_principal_id, action,
            previous_user_id, new_user_id, actor_user_id,
            reason, metadata_json, created_at
        )
        SELECT
            gen_random_uuid(), ep.tenant_id, ep.id, 'unlinked',
            ep.linked_user_id, NULL, NULL,
            'Migration removed an IM identity assignment without provider proof',
            jsonb_build_object('migration', 'im_user_verified_binding_0719'),
            NOW()
        FROM external_principals AS ep
        WHERE ep.linked_user_id IS NOT NULL
          AND ep.binding_method IS NULL
        """
    )
    op.execute(
        """
        UPDATE chat_sessions AS cs
        SET user_id = NULL
        FROM external_principals AS ep
        WHERE cs.external_principal_id = ep.id
          AND ep.linked_user_id IS NOT NULL
          AND ep.binding_method IS NULL
        """
    )
    op.execute(
        """
        UPDATE chat_messages AS cm
        SET user_id = NULL
        FROM external_principals AS ep
        WHERE cm.external_principal_id = ep.id
          AND ep.linked_user_id IS NOT NULL
          AND ep.binding_method IS NULL
        """
    )
    op.execute(
        """
        UPDATE external_principals
        SET linked_user_id = NULL,
            linked_at = NULL,
            binding_method = NULL,
            binding_verified_at = NULL,
            updated_at = NOW()
        WHERE linked_user_id IS NOT NULL
          AND binding_method IS NULL
        """
    )
    op.execute(
        """
        UPDATE channel_configs AS cc
        SET self_identity_user_id = NULL,
            self_identity_verified_at = NULL,
            is_connected = FALSE,
            updated_at = NOW()
        WHERE cc.channel_type = 'wechat_personal'
          AND cc.self_identity_user_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM external_principals AS ep
              WHERE ep.channel_config_id = cc.id
                AND ep.tenant_id = cc.tenant_id
                AND ep.provider = 'wechat_personal'
                AND ep.status = 'active'
                AND ep.linked_user_id = cc.self_identity_user_id
                AND ep.binding_method = 'wechat_qr'
                AND ep.binding_verified_at IS NOT NULL
          )
        """
    )

    # Every pre-migration Feishu/Lark installation lacks the new QR proof.
    # Stop its transport and make the required recovery explicit in Agent
    # Detail. A fresh QR registration creates a new installation and writes the
    # scanner binding in the same transaction as its encrypted credentials.
    op.execute(
        """
        UPDATE channel_configs AS cc
        SET self_identity_user_id = NULL,
            self_identity_verified_at = NULL,
            is_connected = FALSE,
            is_configured = FALSE,
            extra_config = (
                COALESCE(cc.extra_config, '{}'::json)::jsonb
                || jsonb_build_object(
                    'connection_status', 'identity_rebind_required',
                    'identity_status', 'rebind_required'
                )
            )::json,
            updated_at = NOW()
        WHERE cc.channel_type = 'feishu'
          AND NOT EXISTS (
              SELECT 1
              FROM external_principals AS ep
              WHERE ep.channel_config_id = cc.id
                AND ep.tenant_id = cc.tenant_id
                AND ep.provider = 'feishu'
                AND ep.status = 'active'
                AND ep.linked_user_id = cc.self_identity_user_id
                AND ep.binding_method = 'feishu_qr'
                AND ep.binding_verified_at IS NOT NULL
          )
        """
    )

    op.execute(
        "ALTER TABLE external_principals "
        "DROP CONSTRAINT IF EXISTS ck_external_principals_verified_binding"
    )
    op.execute(
        """
        ALTER TABLE external_principals
        ADD CONSTRAINT ck_external_principals_verified_binding
        CHECK (
            linked_user_id IS NULL
            OR (
                linked_at IS NOT NULL
                AND (
                    (provider = 'wechat_personal' AND binding_method = 'wechat_qr')
                    OR (provider = 'feishu' AND binding_method = 'feishu_qr')
                )
                AND binding_verified_at IS NOT NULL
            )
        )
        """
    )

    op.execute(
        "ALTER TABLE channel_configs "
        "DROP CONSTRAINT IF EXISTS ck_channel_configs_self_identity_channel"
    )
    op.execute(
        """
        ALTER TABLE channel_configs
        ADD CONSTRAINT ck_channel_configs_self_identity_channel
        CHECK (
            self_identity_user_id IS NULL
            OR (
                channel_type IN ('wechat_personal','feishu')
                AND self_identity_verified_at IS NOT NULL
            )
        )
        """
    )


def downgrade() -> None:
    # The previous runtime has no Feishu QR-proof contract. Remove those User
    # authority projections before dropping the evidence columns so rollback
    # cannot silently turn a verified binding into an untyped assignment.
    op.execute(
        """
        UPDATE chat_sessions AS cs
        SET user_id = NULL
        FROM external_principals AS ep
        WHERE cs.external_principal_id = ep.id
          AND ep.provider = 'feishu'
          AND ep.linked_user_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE chat_messages AS cm
        SET user_id = NULL
        FROM external_principals AS ep
        WHERE cm.external_principal_id = ep.id
          AND ep.provider = 'feishu'
          AND ep.linked_user_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE external_principals
        SET linked_user_id = NULL,
            linked_at = NULL,
            binding_method = NULL,
            binding_verified_at = NULL,
            updated_at = NOW()
        WHERE provider = 'feishu'
          AND linked_user_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE channel_configs AS cc
        SET self_identity_user_id = NULL,
            self_identity_verified_at = NULL,
            is_connected = FALSE,
            is_configured = FALSE,
            extra_config = (
                COALESCE(cc.extra_config, '{}'::json)::jsonb
                || jsonb_build_object(
                    'connection_status', 'identity_rebind_required',
                    'identity_status', 'rebind_required'
                )
            )::json,
            updated_at = NOW()
        WHERE cc.channel_type = 'feishu'
        """
    )

    # Assignments removed during upgrade had no proof and cannot be recreated.
    # The remaining WeChat projection is compatible with the parent contract.
    op.execute(
        "ALTER TABLE channel_configs "
        "DROP CONSTRAINT IF EXISTS ck_channel_configs_self_identity_channel"
    )
    op.execute(
        """
        ALTER TABLE channel_configs
        ADD CONSTRAINT ck_channel_configs_self_identity_channel
        CHECK (
            self_identity_user_id IS NULL
            OR (
                channel_type = 'wechat_personal'
                AND self_identity_verified_at IS NOT NULL
            )
        )
        """
    )
    op.execute(
        "ALTER TABLE external_principals "
        "DROP CONSTRAINT IF EXISTS ck_external_principals_verified_binding"
    )
    op.execute("ALTER TABLE external_principals DROP COLUMN IF EXISTS binding_verified_at")
    op.execute("ALTER TABLE external_principals DROP COLUMN IF EXISTS binding_method")
