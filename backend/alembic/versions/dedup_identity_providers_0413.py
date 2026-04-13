"""Deduplicate identity_providers and add unique constraint.

Revision ID: dedup_identity_providers_0413
Revises: add_wechat_personal_channel_0411
Create Date: 2026-04-13
"""

from alembic import op

revision = "dedup_identity_providers_0413"
down_revision = "add_wechat_personal_channel_0411"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: Reassign external_identities from duplicate providers to the keeper
    # (keeper = the oldest row per provider_type+tenant_id group)
    op.execute("""
        UPDATE external_identities ei
        SET provider_id = keeper.id
        FROM identity_providers dup
        JOIN (
            SELECT DISTINCT ON (provider_type, tenant_id) id, provider_type, tenant_id
            FROM identity_providers
            ORDER BY provider_type, tenant_id, created_at ASC
        ) keeper ON keeper.provider_type = dup.provider_type
                 AND keeper.tenant_id IS NOT DISTINCT FROM dup.tenant_id
        WHERE ei.provider_id = dup.id
          AND dup.id != keeper.id
    """)

    # Step 2: Delete duplicate providers (keep only the oldest per group)
    op.execute("""
        DELETE FROM identity_providers
        WHERE id NOT IN (
            SELECT DISTINCT ON (provider_type, tenant_id) id
            FROM identity_providers
            ORDER BY provider_type, tenant_id, created_at ASC
        )
    """)

    # Step 3: Add unique constraint to prevent future duplicates
    # Handle NULL tenant_id: PostgreSQL treats NULLs as distinct in unique constraints,
    # so we use a partial unique index for the NULL case
    op.create_unique_constraint(
        "uq_identity_providers_type_tenant",
        "identity_providers",
        ["provider_type", "tenant_id"],
    )
    # Partial index for NULL tenant_id (only one row per provider_type where tenant_id IS NULL)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_providers_type_tenant_null
        ON identity_providers (provider_type)
        WHERE tenant_id IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_identity_providers_type_tenant_null")
    op.drop_constraint("uq_identity_providers_type_tenant", "identity_providers", type_="unique")
