"""Add persistent agent capability install records.

Revision ID: add_agent_capability_installs_0402
Revises: add_runtime_tasks_exec_mode_0401
Create Date: 2026-04-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON, UUID


revision: str = "add_agent_capability_installs_0402"
down_revision: Union[str, None] = "add_runtime_tasks_exec_mode_0401"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    from sqlalchemy import text
    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :table"),
        {"table": table},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _table_exists("agent_capability_installs"):
        op.create_table(
            "agent_capability_installs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kind", sa.String(length=30), nullable=False),
            sa.Column("source_key", sa.String(length=255), nullable=False),
            sa.Column("normalized_key", sa.String(length=255), nullable=False),
            sa.Column("display_name", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("installed_via", sa.String(length=30), nullable=False, server_default="hr_agent"),
            sa.Column("error_code", sa.String(length=80), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("metadata_json", JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("agent_id", "kind", "normalized_key", name="uq_agent_capability_install"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_capability_installs_agent_id ON agent_capability_installs (agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_capability_installs_kind ON agent_capability_installs (kind)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_capability_installs_status ON agent_capability_installs (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_capability_installs_created_at ON agent_capability_installs (created_at)")


def downgrade() -> None:
    op.drop_index("ix_agent_capability_installs_created_at", table_name="agent_capability_installs")
    op.drop_index("ix_agent_capability_installs_status", table_name="agent_capability_installs")
    op.drop_index("ix_agent_capability_installs_kind", table_name="agent_capability_installs")
    op.drop_index("ix_agent_capability_installs_agent_id", table_name="agent_capability_installs")
    op.drop_table("agent_capability_installs")
