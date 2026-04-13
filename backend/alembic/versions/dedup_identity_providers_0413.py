"""Deduplicate identity_providers and add unique constraint.

Revision ID: dedup_identity_providers_0413
Revises: add_wechat_personal_channel_0411
Create Date: 2026-04-13
"""

import sqlalchemy as sa
from alembic import op

revision = "dedup_identity_providers_0413"
down_revision = "add_wechat_personal_channel_0411"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotency guard: if the unique constraint already exists, this migration
    # was previously applied (but alembic_version wasn't updated due to a crash).
    # Skip all operations — the data is already clean.
    conn = op.get_bind()
    already_applied = conn.execute(
        sa.text("""
            SELECT 1 FROM information_schema.table_constraints
            WHERE constraint_name = 'uq_identity_providers_type_tenant'
              AND table_name = 'identity_providers'
        """)
    ).scalar()
    if already_applied:
        return

    # Keeper = oldest provider per (provider_type, tenant_id) group.
    # Duplicate = any other provider in the same group.

    # Step 1: Delete external_identities on DUPLICATE providers that would
    # conflict with an identity already on the KEEPER (same provider_user_id
    # or provider_open_id).
    op.execute("""
        DELETE FROM external_identities ei
        USING identity_providers dup,
              (
                  SELECT DISTINCT ON (provider_type, tenant_id) id, provider_type, tenant_id
                  FROM identity_providers
                  ORDER BY provider_type, tenant_id, created_at ASC
              ) keeper
        WHERE ei.provider_id = dup.id
          AND dup.id != keeper.id
          AND dup.provider_type = keeper.provider_type
          AND dup.tenant_id IS NOT DISTINCT FROM keeper.tenant_id
          AND (
              EXISTS (
                  SELECT 1 FROM external_identities k
                  WHERE k.provider_id = keeper.id
                    AND k.provider_user_id IS NOT NULL
                    AND k.provider_user_id = ei.provider_user_id
              )
              OR EXISTS (
                  SELECT 1 FROM external_identities k
                  WHERE k.provider_id = keeper.id
                    AND k.provider_open_id IS NOT NULL
                    AND k.provider_open_id = ei.provider_open_id
              )
          )
    """)

    # Step 2: Reassign remaining external_identities from duplicate providers
    # to the keeper (no conflict now).
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

    # Step 2b: Reassign org_members from duplicate providers to the keeper.
    op.execute("""
        UPDATE org_members om
        SET provider_id = keeper.id
        FROM identity_providers dup
        JOIN (
            SELECT DISTINCT ON (provider_type, tenant_id) id, provider_type, tenant_id
            FROM identity_providers
            ORDER BY provider_type, tenant_id, created_at ASC
        ) keeper ON keeper.provider_type = dup.provider_type
                 AND keeper.tenant_id IS NOT DISTINCT FROM dup.tenant_id
        WHERE om.provider_id = dup.id
          AND dup.id != keeper.id
    """)

    # Step 3: Delete duplicate providers (keep only the oldest per group).
    op.execute("""
        DELETE FROM identity_providers
        WHERE id NOT IN (
            SELECT DISTINCT ON (provider_type, tenant_id) id
            FROM identity_providers
            ORDER BY provider_type, tenant_id, created_at ASC
        )
    """)

    # Step 4: Add unique constraint to prevent future duplicates.
    op.create_unique_constraint(
        "uq_identity_providers_type_tenant",
        "identity_providers",
        ["provider_type", "tenant_id"],
    )
    # Partial index for NULL tenant_id (PostgreSQL treats NULLs as distinct
    # in regular unique constraints).
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_providers_type_tenant_null
        ON identity_providers (provider_type)
        WHERE tenant_id IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_identity_providers_type_tenant_null")
    op.drop_constraint("uq_identity_providers_type_tenant", "identity_providers", type_="unique")
