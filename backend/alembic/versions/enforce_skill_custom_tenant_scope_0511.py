"""Enforce tenant ownership for custom skills.

Revision ID: enforce_skill_custom_tenant_scope_0511
Revises: scope_skill_registry_uniqueness_0511
"""

from alembic import op


revision = "enforce_skill_custom_tenant_scope_0511"
down_revision = "scope_skill_registry_uniqueness_0511"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Repair deployments that already ran the broader global indexes from the
    # previous migration. Global uniqueness should apply only to builtin skills;
    # tenant custom skills are isolated by (tenant_id, name/folder_name).
    op.execute("DROP INDEX IF EXISTS uq_skills_global_name")
    op.execute("DROP INDEX IF EXISTS uq_skills_global_folder_name")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_global_name
        ON skills (name)
        WHERE tenant_id IS NULL AND is_builtin IS TRUE
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_global_folder_name
        ON skills (folder_name)
        WHERE tenant_id IS NULL AND is_builtin IS TRUE
        """
    )

    # Do not validate existing legacy rows because old unscoped custom skills
    # cannot be assigned to a tenant safely without product ownership input.
    # PostgreSQL still enforces this CHECK for future inserts/updates.
    op.execute("ALTER TABLE skills DROP CONSTRAINT IF EXISTS ck_skills_custom_requires_tenant")
    op.execute(
        """
        ALTER TABLE skills
        ADD CONSTRAINT ck_skills_custom_requires_tenant
        CHECK (tenant_id IS NOT NULL OR is_builtin IS TRUE)
        NOT VALID
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE skills DROP CONSTRAINT IF EXISTS ck_skills_custom_requires_tenant")
    op.execute("DROP INDEX IF EXISTS uq_skills_global_folder_name")
    op.execute("DROP INDEX IF EXISTS uq_skills_global_name")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_global_name
        ON skills (name)
        WHERE tenant_id IS NULL AND is_builtin IS TRUE
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_global_folder_name
        ON skills (folder_name)
        WHERE tenant_id IS NULL AND is_builtin IS TRUE
        """
    )
