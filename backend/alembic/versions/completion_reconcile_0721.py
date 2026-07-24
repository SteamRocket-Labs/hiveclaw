"""Bound terminal completion reconciliation to generation-marked tasks.

Revision ID: completion_reconcile_0721
Revises: query_resource_safety_0721
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa


revision = "completion_reconcile_0721"
down_revision = "query_resource_safety_0721"
branch_labels = None
depends_on = None


_NEW_INDEX = "ix_runtime_tasks_completion_outbox_pending"
_OLD_INDEX = "ix_runtime_tasks_notification_reconcile"
_SUPPORTED_TYPES = (
    "'subagent', 'team_member', 'workflow', 'delegation', 'a2a_delegation', 'trigger', 'approval_execution'"
)
_TERMINAL_STATUSES = "'completed', 'failed', 'killed', 'skipped', 'needs_reconciliation'"
_PENDING_PREDICATE = (
    "completion_outbox_generation IS NOT NULL "
    "AND completion_outbox_settled_at IS NULL "
    f"AND task_type IN ({_SUPPORTED_TYPES}) "
    f"AND status IN ({_TERMINAL_STATUSES}) "
    "AND NOT (task_type = 'trigger' AND status = 'skipped') "
    "AND tenant_id IS NOT NULL AND parent_agent_id IS NOT NULL"
)
_OLD_PREDICATE = (
    f"task_type IN ({_SUPPORTED_TYPES}) "
    f"AND status IN ({_TERMINAL_STATUSES}) "
    "AND tenant_id IS NOT NULL AND parent_agent_id IS NOT NULL"
)


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("runtime_tasks")}


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


def upgrade() -> None:
    columns = _columns()
    if "completion_outbox_generation" not in columns:
        op.add_column(
            "runtime_tasks",
            sa.Column("completion_outbox_generation", sa.SmallInteger(), nullable=True),
        )
    if "completion_outbox_settled_at" not in columns:
        op.add_column(
            "runtime_tasks",
            sa.Column("completion_outbox_settled_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "completion_outbox_attempted_at" not in columns:
        op.add_column(
            "runtime_tasks",
            sa.Column("completion_outbox_attempted_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "completion_outbox_attempt_count" not in columns:
        op.add_column(
            "runtime_tasks",
            sa.Column(
                "completion_outbox_attempt_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    if "completion_outbox_last_error" not in columns:
        op.add_column(
            "runtime_tasks",
            sa.Column("completion_outbox_last_error", sa.String(length=100), nullable=True),
        )

    # Existing terminal rows predate this recovery generation and must not be
    # replayed as fresh user-visible notifications. Only in-flight supported
    # work is adopted; the default covers inserts racing after this statement.
    op.alter_column(
        "runtime_tasks",
        "completion_outbox_generation",
        existing_type=sa.SmallInteger(),
        server_default=sa.text("1"),
    )
    op.execute(
        sa.text(
            "UPDATE runtime_tasks SET completion_outbox_generation=1 "
            "WHERE completion_outbox_generation IS NULL "
            f"AND task_type IN ({_SUPPORTED_TYPES}) "
            "AND status IN ('pending', 'running', 'resumable', 'suspended')"
        )
    )

    with op.get_context().autocommit_block():
        _drop_invalid_index(_NEW_INDEX)
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_NEW_INDEX} "
            "ON runtime_tasks ("
            "(completion_outbox_attempted_at IS NOT NULL), "
            "completion_outbox_attempted_at ASC, "
            "created_at ASC"
            f") WHERE {_PENDING_PREDICATE}"
        )
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_OLD_INDEX}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _drop_invalid_index(_OLD_INDEX)
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_OLD_INDEX} "
            "ON runtime_tasks (completed_at DESC NULLS LAST, created_at DESC) "
            f"WHERE {_OLD_PREDICATE}"
        )
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NEW_INDEX}")

    columns = _columns()
    for column in (
        "completion_outbox_last_error",
        "completion_outbox_attempt_count",
        "completion_outbox_attempted_at",
        "completion_outbox_settled_at",
        "completion_outbox_generation",
    ):
        if column in columns:
            op.drop_column("runtime_tasks", column)
