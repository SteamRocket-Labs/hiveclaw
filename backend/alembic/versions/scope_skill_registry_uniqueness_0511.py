"""Scope skill registry uniqueness by tenant.

Revision ID: scope_skill_registry_uniqueness_0511
Revises: add_unique_system_hr_agent_per_tenant_0501
"""

from alembic import op


revision = "scope_skill_registry_uniqueness_0511"
down_revision = "add_unique_system_hr_agent_per_tenant_0501"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier schema used global unique constraints for name/folder_name even
    # after tenant_id was added. Keep global builtin names unique, but allow
    # different tenants to install/create the same folder names independently.
    op.execute("ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_name_key")
    op.execute("ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_folder_name_key")

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
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_tenant_name
        ON skills (tenant_id, name)
        WHERE tenant_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_tenant_folder_name
        ON skills (tenant_id, folder_name)
        WHERE tenant_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_skills_tenant_folder_name")
    op.execute("DROP INDEX IF EXISTS uq_skills_tenant_name")
    op.execute("DROP INDEX IF EXISTS uq_skills_global_folder_name")
    op.execute("DROP INDEX IF EXISTS uq_skills_global_name")
    op.execute("ALTER TABLE skills ADD CONSTRAINT skills_folder_name_key UNIQUE (folder_name)")
    op.execute("ALTER TABLE skills ADD CONSTRAINT skills_name_key UNIQUE (name)")
