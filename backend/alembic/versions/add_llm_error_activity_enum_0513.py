"""Add LLM error activity action type.

Revision ID: add_llm_error_activity_enum_0513
Revises: enforce_skill_custom_tenant_scope_0511
"""

from alembic import op


revision = "add_llm_error_activity_enum_0513"
down_revision = "enforce_skill_custom_tenant_scope_0511"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS 'llm_error'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely. Downgraded code should
    # stop writing llm_error, but existing rows remain valid.
    pass
