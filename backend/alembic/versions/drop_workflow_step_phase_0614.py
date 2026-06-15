"""Retire dead WorkflowStep.phase column (Step 10).

``workflow_steps.phase`` was added in ``add_workflow_tables_0604`` but never
read or written by any code path: ``step_type`` carries the step kind, and a
grep of ``.phase`` / ``phase=`` across ``app/`` is empty. The column has only
ever held NULL, so no data archival is needed (unlike
``retire_trigger_focus_ref_0613``) — downgrade simply re-adds the empty column.

``DROP COLUMN IF EXISTS`` keeps this idempotent across both deployment paths:
the migration path (and any pre-existing create_all database) carries the
column and drops it; a fresh create_all deployment never builds it (the model
no longer maps ``phase``) and the IF EXISTS is a no-op. Pre-existing
create_all production databases that stamp head and skip migrations retain a
harmless NULL-only column the ORM no longer references — documented in the
Step 10 rollback notes.

Revision ID: drop_workflow_step_phase_0614
Revises: add_installed_plugin_tables_0614
Create Date: 2026-06-14
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "drop_workflow_step_phase_0614"
down_revision: Union[str, None] = "add_installed_plugin_tables_0614"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE workflow_steps DROP COLUMN IF EXISTS phase"))


def downgrade() -> None:
    op.execute(text("ALTER TABLE workflow_steps ADD COLUMN IF NOT EXISTS phase VARCHAR(100)"))
