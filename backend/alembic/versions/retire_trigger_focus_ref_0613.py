"""Retire trigger focus_ref after focus.md removal.

Revision ID: retire_trigger_focus_ref_0613
Revises: invocation_spans_0613
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "retire_trigger_focus_ref_0613"
down_revision: Union[str, None] = "invocation_spans_0613"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = :table_name
              AND column_name = :column_name
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _column_exists("agent_triggers", "focus_ref"):
        return

    conn = op.get_bind()
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS retired_trigger_focus_refs_0613 (
                trigger_id UUID PRIMARY KEY,
                agent_id UUID,
                tenant_id UUID,
                focus_ref VARCHAR(200),
                archived_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO retired_trigger_focus_refs_0613 (trigger_id, agent_id, tenant_id, focus_ref)
            SELECT id, agent_id, tenant_id, focus_ref
            FROM agent_triggers
            WHERE focus_ref IS NOT NULL AND btrim(focus_ref) <> ''
            ON CONFLICT (trigger_id) DO NOTHING
            """
        )
    )
    conn.execute(text("ALTER TABLE agent_triggers DROP COLUMN IF EXISTS focus_ref"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("ALTER TABLE agent_triggers ADD COLUMN IF NOT EXISTS focus_ref VARCHAR(200)"))
    conn.execute(
        text(
            """
            UPDATE agent_triggers AS trigger
            SET focus_ref = archived.focus_ref
            FROM retired_trigger_focus_refs_0613 AS archived
            WHERE trigger.id = archived.trigger_id
              AND trigger.focus_ref IS NULL
            """
        )
    )
