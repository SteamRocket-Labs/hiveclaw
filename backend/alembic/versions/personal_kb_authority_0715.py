"""Bind Personal KB grants to requester/session/purpose and quarantine legacy grants.

Revision ID: personal_kb_authority_0715
Revises: personal_kb_sensitivity_canonical_0715
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "personal_kb_authority_0715"
down_revision = "personal_kb_sensitivity_canonical_0715"
branch_labels = None
depends_on = None


_RECOVERY_KEY = "_personal_kb_authority_0715_original_metadata"


def _set_rls(*, enabled: bool) -> None:
    action = "ENABLE" if enabled else "DISABLE"
    op.execute(f"ALTER TABLE knowledge_grants {action} ROW LEVEL SECURITY")
    if enabled:
        op.execute("ALTER TABLE knowledge_grants FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    _set_rls(enabled=False)

    op.add_column(
        "knowledge_grants",
        sa.Column(
            "requester_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("knowledge_grants", sa.Column("session_id", sa.String(length=255), nullable=True))
    op.add_column("knowledge_grants", sa.Column("purpose", sa.String(length=80), nullable=True))
    op.add_column("knowledge_grants", sa.Column("delegation_id", sa.String(length=255), nullable=True))
    op.add_column(
        "knowledge_grants",
        sa.Column("sensitivity_ceiling", sa.String(length=30), nullable=True),
    )
    op.add_column("knowledge_grants", sa.Column("binding_key", sa.String(length=200), nullable=True))
    op.add_column("knowledge_grants", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "knowledge_grants",
        sa.Column(
            "revoked_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # A legacy grant has no authenticated requester/session/purpose/ceiling.
    # Preserve its exact metadata, but never guess intent or keep it active.
    op.execute(
        sa.text(
            f"""
            UPDATE knowledge_grants
            SET grant_metadata_json = jsonb_set(
                    jsonb_set(
                        COALESCE(grant_metadata_json, '{{}}'::jsonb),
                        '{{{_RECOVERY_KEY}}}',
                        COALESCE(grant_metadata_json, '{{}}'::jsonb),
                        true
                    ),
                    '{{authority_status}}',
                    to_jsonb('legacy_authority_unverifiable'::text),
                    true
                ),
                purpose = 'legacy_quarantined',
                sensitivity_ceiling = 'PL1_public',
                binding_key = 'legacy:' || id::text,
                revoked_at = COALESCE(revoked_at, now())
            WHERE binding_key IS NULL
            """
        )
    )

    op.alter_column(
        "knowledge_grants",
        "sensitivity_ceiling",
        existing_type=sa.String(length=30),
        nullable=False,
        server_default="PL1_public",
    )
    op.alter_column(
        "knowledge_grants",
        "binding_key",
        existing_type=sa.String(length=200),
        nullable=False,
        server_default=sa.text("concat('compat:', md5(random()::text || clock_timestamp()::text))"),
    )

    op.drop_constraint("uq_knowledge_grant_resource_grantee", "knowledge_grants", type_="unique")
    op.create_unique_constraint(
        "uq_knowledge_grant_resource_grantee",
        "knowledge_grants",
        [
            "tenant_id",
            "scope_type",
            "scope_id",
            "resource_type",
            "resource_id",
            "grantee_type",
            "grantee_id",
            "permission",
            "binding_key",
        ],
    )
    op.create_check_constraint(
        "ck_knowledge_grant_sensitivity_ceiling",
        "knowledge_grants",
        "sensitivity_ceiling IN ('PL1_public','PL2_pii','PL3_sensitive','PL4_credential')",
    )
    op.create_check_constraint(
        "ck_knowledge_grant_agent_binding",
        "knowledge_grants",
        "grantee_type != 'agent' OR revoked_at IS NOT NULL OR ("
        "requester_user_id IS NOT NULL AND expires_at IS NOT NULL "
        "AND ((purpose = 'autonomous_agent' AND requester_user_id = scope_id "
        "AND session_id IS NULL AND delegation_id IS NULL) "
        "OR (purpose = 'interactive_session' AND session_id IS NOT NULL AND delegation_id IS NULL) "
        "OR (purpose IN ('a2a_delegation','subagent_delegation') "
        "AND session_id IS NOT NULL AND delegation_id IS NOT NULL)))",
    )
    op.create_check_constraint(
        "ck_knowledge_grant_resource_binding",
        "knowledge_grants",
        "revoked_at IS NOT NULL OR ((resource_type = 'scope' AND resource_id = scope_id "
        "AND document_id IS NULL) OR (resource_type = 'document' AND document_id IS NOT NULL "
        "AND resource_id = document_id))",
    )
    op.create_check_constraint(
        "ck_knowledge_grant_revoke_actor",
        "knowledge_grants",
        "revoked_by_user_id IS NULL OR revoked_at IS NOT NULL",
    )

    for column in (
        "requester_user_id",
        "session_id",
        "purpose",
        "delegation_id",
        "sensitivity_ceiling",
        "binding_key",
        "revoked_at",
        "revoked_by_user_id",
    ):
        op.create_index(f"ix_knowledge_grants_{column}", "knowledge_grants", [column], unique=False)

    _set_rls(enabled=True)


def downgrade() -> None:
    _set_rls(enabled=False)

    for column in reversed(
        (
            "requester_user_id",
            "session_id",
            "purpose",
            "delegation_id",
            "sensitivity_ceiling",
            "binding_key",
            "revoked_at",
            "revoked_by_user_id",
        )
    ):
        op.drop_index(f"ix_knowledge_grants_{column}", table_name="knowledge_grants")

    op.drop_constraint("ck_knowledge_grant_revoke_actor", "knowledge_grants", type_="check")
    op.drop_constraint("ck_knowledge_grant_resource_binding", "knowledge_grants", type_="check")
    op.drop_constraint("ck_knowledge_grant_agent_binding", "knowledge_grants", type_="check")
    op.drop_constraint("ck_knowledge_grant_sensitivity_ceiling", "knowledge_grants", type_="check")
    op.drop_constraint("uq_knowledge_grant_resource_grantee", "knowledge_grants", type_="unique")
    op.create_unique_constraint(
        "uq_knowledge_grant_resource_grantee",
        "knowledge_grants",
        [
            "tenant_id",
            "scope_type",
            "scope_id",
            "resource_type",
            "resource_id",
            "grantee_type",
            "grantee_id",
            "permission",
        ],
    )

    # Old readers know only expiry. Preserve evidence and force every restored
    # edge inactive so rollback cannot reopen the vulnerability.
    op.execute(
        sa.text(
            f"""
            UPDATE knowledge_grants
            SET grant_metadata_json = jsonb_set(
                    COALESCE(grant_metadata_json -> '{_RECOVERY_KEY}', grant_metadata_json, '{{}}'::jsonb),
                    '{{authority_status}}',
                    to_jsonb('downgrade_quarantined'::text),
                    true
                ),
                expires_at = LEAST(COALESCE(expires_at, now()), now())
            """
        )
    )

    for column in reversed(
        (
            "requester_user_id",
            "session_id",
            "purpose",
            "delegation_id",
            "sensitivity_ceiling",
            "binding_key",
            "revoked_at",
            "revoked_by_user_id",
        )
    ):
        op.drop_column("knowledge_grants", column)

    _set_rls(enabled=True)
