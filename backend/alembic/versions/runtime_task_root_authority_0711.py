"""Bind RuntimeTask control to root user, session, and delegation chain.

Revision ID: runtime_task_root_authority_0711
Revises: channel_delivery_outbox_0711
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.migration_compat import add_column_if_missing, create_check_constraint_if_missing


revision = "runtime_task_root_authority_0711"
down_revision = "channel_delivery_outbox_0711"
branch_labels = None
depends_on = None


_BATCH_SIZE = 10_000


def _backfill_root_authority() -> None:
    bind = op.get_bind()
    while True:
        updated_count = bind.execute(
            sa.text(
                r"""
                WITH batch AS (
                    SELECT id
                    FROM runtime_tasks
                    WHERE delegation_chain_json IS NULL
                    ORDER BY id
                    LIMIT :batch_size
                    FOR UPDATE SKIP LOCKED
                ),
                updated AS (
                UPDATE runtime_tasks AS rt
                SET root_user_id = COALESCE(
                        CASE
                            WHEN COALESCE(rt.metadata_json #>> '{execution_principal,requester_user_id}', '')
                                 ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                            THEN (rt.metadata_json #>> '{execution_principal,requester_user_id}')::uuid
                        END,
                        CASE
                            WHEN COALESCE(rt.metadata_json ->> 'root_user_id', '')
                                 ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                            THEN (rt.metadata_json ->> 'root_user_id')::uuid
                        END,
                        (SELECT budget.root_user_id FROM runtime_budget_runs AS budget WHERE budget.id = rt.budget_run_id),
                        (
                            SELECT session.user_id
                            FROM chat_sessions AS session
                            WHERE session.id::text = rt.parent_session_id
                            LIMIT 1
                        )
                    ),
                    root_session_id = COALESCE(
                        NULLIF(rt.metadata_json #>> '{execution_principal,root_session_id}', ''),
                        NULLIF(rt.metadata_json ->> 'root_session_id', ''),
                        (SELECT budget.root_session_id FROM runtime_budget_runs AS budget WHERE budget.id = rt.budget_run_id),
                        (
                            SELECT COALESCE(session.root_session_id::text, session.parent_session_id::text, session.id::text)
                            FROM chat_sessions AS session
                            WHERE session.id::text = rt.parent_session_id
                            LIMIT 1
                        ),
                        NULLIF(rt.parent_session_id, '')
                    ),
                    delegation_chain_json = CASE
                        WHEN jsonb_typeof((rt.metadata_json::jsonb) -> 'delegation_chain') = 'array'
                             AND jsonb_array_length((rt.metadata_json::jsonb) -> 'delegation_chain') > 0
                        THEN (rt.metadata_json::jsonb) -> 'delegation_chain'
                        WHEN rt.parent_agent_id IS NULL THEN '[]'::jsonb
                        WHEN rt.task_type = 'subagent' THEN jsonb_build_array(
                            'agent:' || rt.parent_agent_id::text,
                            'subagent:' || rt.id::text || ':' || COALESCE(NULLIF(rt.child_agent_name, ''), 'worker')
                        )
                        WHEN rt.child_agent_id IS NOT NULL THEN jsonb_build_array(
                            'agent:' || rt.parent_agent_id::text,
                            'agent:' || rt.child_agent_id::text
                        )
                        ELSE jsonb_build_array('agent:' || rt.parent_agent_id::text)
                    END
                FROM batch
                WHERE rt.id = batch.id
                RETURNING 1
                )
                SELECT count(*) FROM updated
                """
            ),
            {"batch_size": _BATCH_SIZE},
        ).scalar_one()
        if int(updated_count or 0) == 0:
            return


def upgrade() -> None:
    add_column_if_missing(
        op,
        "runtime_tasks",
        sa.Column("root_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    add_column_if_missing(
        op,
        "runtime_tasks",
        sa.Column("root_session_id", sa.String(length=512), nullable=True),
    )
    add_column_if_missing(
        op,
        "runtime_tasks",
        sa.Column("delegation_chain_json", postgresql.JSONB(), nullable=True),
    )
    # Existing rows intentionally remain NULL as the backfill work queue;
    # rolling old-version INSERTs immediately receive the compatibility value.
    op.alter_column(
        "runtime_tasks",
        "delegation_chain_json",
        existing_type=postgresql.JSONB(),
        server_default=sa.text("'[]'::jsonb"),
    )

    # Backfill from the strongest mechanical truth available, in order:
    # immutable execution principal, explicit metadata, BudgetRun root, then
    # the persisted parent ChatSession.  Historical owner_id is intentionally
    # not trusted: older A2A rows used Agent.creator_id and cannot prove the
    # actual requester.
    with op.get_context().autocommit_block():
        _backfill_root_authority()
        create_check_constraint_if_missing(
            op,
            "ck_runtime_tasks_delegation_chain_not_null",
            "runtime_tasks",
            "delegation_chain_json IS NOT NULL",
            postgresql_not_valid=True,
        )
        op.execute("ALTER TABLE runtime_tasks VALIDATE CONSTRAINT ck_runtime_tasks_delegation_chain_not_null")
    op.alter_column(
        "runtime_tasks",
        "delegation_chain_json",
        existing_type=postgresql.JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    )
    op.drop_constraint("ck_runtime_tasks_delegation_chain_not_null", "runtime_tasks", type_="check")
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_runtime_tasks_root_user_id ON runtime_tasks (root_user_id)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_runtime_tasks_root_session_id "
            "ON runtime_tasks (root_session_id)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_runtime_tasks_root_authority "
            "ON runtime_tasks (tenant_id, parent_agent_id, root_user_id, root_session_id)"
        )


def downgrade() -> None:
    op.drop_index("ix_runtime_tasks_root_authority", table_name="runtime_tasks")
    op.drop_index("ix_runtime_tasks_root_session_id", table_name="runtime_tasks")
    op.drop_index("ix_runtime_tasks_root_user_id", table_name="runtime_tasks")
    op.drop_column("runtime_tasks", "delegation_chain_json")
    op.drop_column("runtime_tasks", "root_session_id")
    op.drop_column("runtime_tasks", "root_user_id")
