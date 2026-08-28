"""Add the durable A2A continuation RuntimeTask type.

Revision ID: a2a_continuation_task_0828
Revises: merge_incident_kimi_0725
Create Date: 2026-08-28

A follow-up sent to an existing A2A delegation child session starts an
executable-chat successor run. Typing that successor ``web_chat_turn`` made it
permanently invisible to the completion-outbox recovery predicate, so the
parent was never woken (production defect DAY1-A2A-CONT-RETURN-001). The
dedicated ``a2a_continuation`` type executes through the same web-chat lane
but stays completion-outbox eligible. This migration rewrites four contracts.
The two check constraints and the active-run unique index change inside the
migration transaction; the outbox pending index is replaced online in an
autocommit block via concurrent replacement-rename (retry-safe) and is
deliberately NOT atomic with the rest — PostgreSQL concurrent index DDL
cannot run inside a transaction:

1. ``ck_runtime_tasks_task_type`` admits the new type.
2. ``ck_runtime_notification_outbox_source_kind`` admits the matching
   completion-outbox source kind.
3. ``uq_runtime_tasks_active_web_chat_session`` guards one active turn per
   session across all five executable-chat types.
4. ``ix_runtime_tasks_completion_outbox_pending`` covers the new type
   (online, concurrent replacement-rename in an autocommit block,
   retry-safe).

No data backfill is required: the type is only written by new code paths, and
historical ``web_chat_turn`` continuation rows keep their existing semantics.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a2a_continuation_task_0828"
down_revision = "merge_incident_kimi_0725"
branch_labels = None
depends_on = None


_RUNTIME_TASK_TYPES = (
    "web_chat_turn",
    "goal_continuation",
    "team_member",
    "advanced_plan",
    "workflow",
    "delegation",
    "business_task",
    "subagent",
    "trigger",
    "heartbeat",
    "coordinator_worker",
    "harness_canary",
    "a2a_delegation",
    "approval_execution",
    "hr_provisioning",
    "dream",
    "a2a_continuation",
)
_LEGACY_RUNTIME_TASK_TYPES = _RUNTIME_TASK_TYPES[:-1]

_OUTBOX_INDEX = "ix_runtime_tasks_completion_outbox_pending"
_OUTBOX_REPLACEMENT_INDEX = "ix_runtime_tasks_completion_outbox_pending_a2a_new"
_OUTBOX_LEGACY_REPLACEMENT_INDEX = "ix_runtime_tasks_completion_outbox_pending_a2a_old"
_OUTBOX_SUPPORTED_TYPES = (
    "'subagent', 'team_member', 'workflow', 'delegation', 'a2a_delegation', "
    "'a2a_continuation', 'trigger', 'approval_execution'"
)
_OUTBOX_LEGACY_SUPPORTED_TYPES = (
    "'subagent', 'team_member', 'workflow', 'delegation', 'a2a_delegation', 'trigger', 'approval_execution'"
)
_OUTBOX_TERMINAL_STATUSES = "'completed', 'failed', 'killed', 'skipped', 'needs_reconciliation'"
_OUTBOX_PENDING_PREDICATE = (
    "completion_outbox_generation IS NOT NULL "
    "AND completion_outbox_settled_at IS NULL "
    f"AND task_type IN ({_OUTBOX_SUPPORTED_TYPES}) "
    f"AND status IN ({_OUTBOX_TERMINAL_STATUSES}) "
    "AND NOT (task_type = 'trigger' AND status = 'skipped') "
    "AND parent_agent_id IS NOT NULL"
)
_OUTBOX_LEGACY_PENDING_PREDICATE = (
    "completion_outbox_generation IS NOT NULL "
    "AND completion_outbox_settled_at IS NULL "
    f"AND task_type IN ({_OUTBOX_LEGACY_SUPPORTED_TYPES}) "
    f"AND status IN ({_OUTBOX_TERMINAL_STATUSES}) "
    "AND NOT (task_type = 'trigger' AND status = 'skipped') "
    "AND parent_agent_id IS NOT NULL"
)

_OUTBOX_SOURCE_KINDS = (
    "'subagent', 'agent_team', 'workflow', 'trigger', 'delegation', 'a2a_delegation', "
    "'a2a_continuation', 'runtime_budget', 'approval'"
)
_OUTBOX_LEGACY_SOURCE_KINDS = (
    "'subagent', 'agent_team', 'workflow', 'trigger', 'delegation', 'a2a_delegation', 'runtime_budget', 'approval'"
)

_ACTIVE_RUN_INDEX = "uq_runtime_tasks_active_web_chat_session"
_ACTIVE_RUN_TYPES = "('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan', 'a2a_continuation')"
_ACTIVE_RUN_LEGACY_TYPES = "('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan')"
_ACTIVE_RUN_STATUSES = "('pending', 'running', 'suspended', 'resumable')"


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _ensure_runtime_task_type_constraint() -> None:
    bind = op.get_bind()
    definition = bind.scalar(
        sa.text(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'runtime_tasks'::regclass
              AND conname = 'ck_runtime_tasks_task_type'
            """
        )
    )
    if definition and all(task_type in str(definition) for task_type in _RUNTIME_TASK_TYPES):
        return
    if definition:
        op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.create_check_constraint(
        "ck_runtime_tasks_task_type",
        "runtime_tasks",
        f"task_type IN ({_quoted(_RUNTIME_TASK_TYPES)})",
    )


def _replace_active_run_unique_index(*, task_types: str) -> None:
    op.execute(f"DROP INDEX IF EXISTS {_ACTIVE_RUN_INDEX}")
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_ACTIVE_RUN_INDEX} "
        "ON runtime_tasks (parent_agent_id, parent_session_id) "
        f"WHERE task_type IN {task_types} "
        f"AND status IN {_ACTIVE_RUN_STATUSES} "
        "AND parent_agent_id IS NOT NULL "
        "AND parent_session_id IS NOT NULL"
    )


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


def _replace_outbox_index(*, replacement: str, predicate: str) -> None:
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
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_OUTBOX_INDEX}")
    op.execute(f"ALTER INDEX {replacement} RENAME TO {_OUTBOX_INDEX}")


def _replace_outbox_source_kind_constraint(source_kinds: str) -> None:
    op.execute(
        "ALTER TABLE runtime_notification_outbox DROP CONSTRAINT IF EXISTS ck_runtime_notification_outbox_source_kind"
    )
    op.execute(
        "ALTER TABLE runtime_notification_outbox "
        "ADD CONSTRAINT ck_runtime_notification_outbox_source_kind "
        f"CHECK (source_kind IN ({source_kinds}))"
    )


def upgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    _ensure_runtime_task_type_constraint()
    _replace_outbox_source_kind_constraint(_OUTBOX_SOURCE_KINDS)
    _replace_active_run_unique_index(task_types=_ACTIVE_RUN_TYPES)
    with op.get_context().autocommit_block():
        _replace_outbox_index(replacement=_OUTBOX_REPLACEMENT_INDEX, predicate=_OUTBOX_PENDING_PREDICATE)
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_OUTBOX_LEGACY_REPLACEMENT_INDEX}")


def downgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.execute("ALTER TABLE runtime_tasks DISABLE ROW LEVEL SECURITY")
    op.execute("DELETE FROM runtime_tasks WHERE task_type = 'a2a_continuation'")
    op.execute("DELETE FROM runtime_notification_outbox WHERE source_kind = 'a2a_continuation'")
    op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.create_check_constraint(
        "ck_runtime_tasks_task_type",
        "runtime_tasks",
        f"task_type IN ({_quoted(_LEGACY_RUNTIME_TASK_TYPES)})",
    )
    op.execute("ALTER TABLE runtime_tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks FORCE ROW LEVEL SECURITY")
    _replace_outbox_source_kind_constraint(_OUTBOX_LEGACY_SOURCE_KINDS)
    _replace_active_run_unique_index(task_types=_ACTIVE_RUN_LEGACY_TYPES)
    with op.get_context().autocommit_block():
        _replace_outbox_index(
            replacement=_OUTBOX_LEGACY_REPLACEMENT_INDEX,
            predicate=_OUTBOX_LEGACY_PENDING_PREDICATE,
        )
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_OUTBOX_REPLACEMENT_INDEX}")
