"""Stop legacy WeChat transports that have no provider proof.

Revision ID: im_unverified_transport_0719
Revises: im_user_verified_binding_0719
Create Date: 2026-07-19
"""

from __future__ import annotations

from alembic import op


revision = "im_unverified_transport_0719"
down_revision = "im_user_verified_binding_0719"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A transport can remain connected only when the exact installation owns
    # an active, provider-proven scanner binding matching its self projection.
    # This deliberately includes legacy configs whose projection was NULL:
    # transport connectivity is not identity proof and must not remain a
    # misleading half-connected state.
    op.execute(
        """
        UPDATE channel_configs AS cc
        SET self_identity_user_id = NULL,
            self_identity_verified_at = NULL,
            is_connected = FALSE,
            extra_config = (
                COALESCE(cc.extra_config, '{}'::json)::jsonb
                || jsonb_build_object(
                    'connection_status', 'identity_rebind_required',
                    'identity_status', 'rebind_required',
                    'requires_rebind', TRUE
                )
            )::json,
            updated_at = NOW()
        WHERE cc.channel_type = 'wechat_personal'
          AND NOT EXISTS (
              SELECT 1
              FROM external_principals AS ep
              WHERE ep.channel_config_id = cc.id
                AND ep.tenant_id = cc.tenant_id
                AND ep.provider = 'wechat_personal'
                AND ep.status = 'active'
                AND ep.subject_id = NULLIF(cc.extra_config ->> 'ilink_user_id', '')
                AND ep.linked_user_id = cc.self_identity_user_id
                AND ep.binding_method = 'wechat_qr'
                AND ep.binding_verified_at IS NOT NULL
          )
        """
    )


def downgrade() -> None:
    # Do not resurrect an unverified long-poll transport during rollback. The
    # encrypted credentials remain available and a fresh QR scan is the safe,
    # recoverable path on both the old and new runtimes.
    pass
