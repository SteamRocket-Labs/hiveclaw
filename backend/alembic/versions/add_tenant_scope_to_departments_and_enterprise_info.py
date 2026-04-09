"""Add tenant scoping to legacy departments and enterprise info.

Revision ID: tenant_scope_align_0325
Revises: merge_tenant_heads_20260325
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "tenant_scope_align_0325"
down_revision: Union[str, Sequence[str], None] = "merge_tenant_heads_20260325"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_settings (
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            key VARCHAR(100) NOT NULL,
            value JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (tenant_id, key)
        )
        """
    )

    op.execute("ALTER TABLE departments ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id)")
    op.execute("ALTER TABLE enterprise_info ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_departments_tenant_id ON departments (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_enterprise_info_tenant_id ON enterprise_info (tenant_id)")

    op.execute(
        """
        WITH first_tenant AS (
            SELECT id
            FROM tenants
            ORDER BY created_at NULLS LAST, id
            LIMIT 1
        ),
        dept_owner AS (
            SELECT
                d.id AS dept_id,
                COALESCE(
                    (
                        SELECT u.tenant_id
                        FROM users u
                        WHERE u.department_id = d.id AND u.tenant_id IS NOT NULL
                        LIMIT 1
                    ),
                    (SELECT id FROM first_tenant)
                ) AS tenant_id
            FROM departments d
        )
        UPDATE departments AS d
        SET tenant_id = dept_owner.tenant_id
        FROM dept_owner
        WHERE d.id = dept_owner.dept_id
          AND d.tenant_id IS NULL
        """
    )

    op.execute(
        """
        WITH first_tenant AS (
            SELECT id
            FROM tenants
            ORDER BY created_at NULLS LAST, id
            LIMIT 1
        )
        UPDATE enterprise_info
        SET tenant_id = (SELECT id FROM first_tenant)
        WHERE tenant_id IS NULL
        """
    )

    op.execute(
        """
        WITH first_tenant AS (
            SELECT id
            FROM tenants
            ORDER BY created_at NULLS LAST, id
            LIMIT 1
        )
        INSERT INTO tenant_settings (tenant_id, key, value, updated_at)
        SELECT
            (SELECT id FROM first_tenant),
            'feishu_org_sync',
            ss.value,
            NOW()
        FROM system_settings ss
        WHERE ss.key = 'feishu_org_sync'
        ON CONFLICT (tenant_id, key) DO UPDATE
        SET value = EXCLUDED.value,
            updated_at = EXCLUDED.updated_at
        """
    )

    op.execute("ALTER TABLE enterprise_info DROP CONSTRAINT IF EXISTS enterprise_info_info_type_key")
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_enterprise_info_tenant_info_type') THEN
                ALTER TABLE enterprise_info ADD CONSTRAINT uq_enterprise_info_tenant_info_type UNIQUE (tenant_id, info_type);
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.drop_constraint("uq_enterprise_info_tenant_info_type", "enterprise_info", type_="unique")
    op.drop_constraint("fk_enterprise_info_tenant_id_tenants", "enterprise_info", type_="foreignkey")
    op.drop_constraint("fk_departments_tenant_id_tenants", "departments", type_="foreignkey")
    op.drop_index("ix_enterprise_info_tenant_id", table_name="enterprise_info")
    op.drop_index("ix_departments_tenant_id", table_name="departments")
    op.drop_column("enterprise_info", "tenant_id")
    op.drop_column("departments", "tenant_id")
    op.create_unique_constraint("enterprise_info_info_type_key", "enterprise_info", ["info_type"])
