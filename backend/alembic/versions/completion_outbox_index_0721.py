"""Make the completion recovery partial index planner-eligible.

Revision ID: completion_outbox_index_0721
Revises: completion_reconcile_0721
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa


revision = "completion_outbox_index_0721"
down_revision = "completion_reconcile_0721"
branch_labels = None
depends_on = None


_INDEX = "ix_runtime_tasks_completion_outbox_pending"
_REPLACEMENT_INDEX = "ix_runtime_tasks_completion_outbox_pending_replacement"
_LEGACY_REPLACEMENT_INDEX = "ix_runtime_tasks_completion_outbox_pending_legacy"
_SUPPORTED_TYPES = (
    "'subagent', 'team_member', 'workflow', 'delegation', 'a2a_delegation', 'trigger', 'approval_execution'"
)
_TERMINAL_STATUSES = "'completed', 'failed', 'killed', 'skipped', 'needs_reconciliation'"

# runtime_tasks.tenant_id is NOT NULL. Keeping that redundant clause in a
# partial-index predicate made the index ineligible after PostgreSQL folded the
# same tautology out of the production query. parent_agent_id remains nullable
# and therefore remains an eligibility condition.
_PENDING_PREDICATE = (
    "completion_outbox_generation IS NOT NULL "
    "AND completion_outbox_settled_at IS NULL "
    f"AND task_type IN ({_SUPPORTED_TYPES}) "
    f"AND status IN ({_TERMINAL_STATUSES}) "
    "AND NOT (task_type = 'trigger' AND status = 'skipped') "
    "AND parent_agent_id IS NOT NULL"
)
_LEGACY_PENDING_PREDICATE = f"{_PENDING_PREDICATE} AND tenant_id IS NOT NULL"


def _drop_invalid_index(name: str) -> None:
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_index i "
                "JOIN pg_class c ON c.oid=i.indexrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relname=:name "
                "AND (NOT i.indisvalid OR NOT i.indisready)"
            ),
            {"name": name},
        )
        .scalar()
    )
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def _replace_index(*, replacement: str, predicate: str) -> None:
    _drop_invalid_index(replacement)
    op.execute(
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {replacement} "
        "ON runtime_tasks ("
        "(completion_outbox_attempted_at IS NOT NULL), "
        "completion_outbox_attempted_at ASC, "
        "created_at ASC"
        f") WHERE {predicate}"
    )
    # Build the usable replacement before removing the official index. This
    # keeps the recovery lane indexed throughout normal deploys and remains
    # retry-safe if Alembic dies between concurrent DDL and its receipt write.
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    op.execute(f"ALTER INDEX {replacement} RENAME TO {_INDEX}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        _replace_index(replacement=_REPLACEMENT_INDEX, predicate=_PENDING_PREDICATE)
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_LEGACY_REPLACEMENT_INDEX}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _replace_index(replacement=_LEGACY_REPLACEMENT_INDEX, predicate=_LEGACY_PENDING_PREDICATE)
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_REPLACEMENT_INDEX}")
