"""Make business Task and RuntimeTask one atomic lifecycle.

Revision ID: business_task_atomic_state_0711
Revises: approval_execution_envelope_0711
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "business_task_atomic_state_0711"
down_revision = "approval_execution_envelope_0711"
branch_labels = None
depends_on = None

BACKFILL_SOURCE = "legacy_business_task_backfill"


def upgrade() -> None:
    # The historical PostgreSQL enum cannot represent honest terminal failures.
    # Convert it to a constrained string so forward and rollback transitions are
    # explicit rather than accumulating irreversible enum values.
    op.execute("ALTER TABLE tasks ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TABLE tasks ALTER COLUMN status TYPE VARCHAR(32) USING status::text")
    op.execute("DROP TYPE IF EXISTS task_status_enum")
    op.create_check_constraint(
        "ck_tasks_status",
        "tasks",
        "status IN ('pending','doing','done','blocked','failed','cancelled','needs_reconciliation')",
    )
    op.alter_column("tasks", "status", server_default="pending")

    op.add_column("tasks", sa.Column("request_id", sa.String(length=128), nullable=True))
    op.add_column("tasks", sa.Column("request_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("active_runtime_task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("execution_attempt", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("tasks", sa.Column("last_execution_status", sa.String(length=32), nullable=True))
    op.add_column("tasks", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("last_result", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_tasks_active_runtime_task_id_runtime_tasks",
        "tasks",
        "runtime_tasks",
        ["active_runtime_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_tasks_active_runtime_task_id", "tasks", ["active_runtime_task_id"])
    op.create_index("ix_tasks_last_execution_status", "tasks", ["last_execution_status"])

    op.execute(
        """
        UPDATE tasks
        SET request_id = 'legacy:' || id::text,
            request_hash = md5('legacy-task:' || id::text),
            last_execution_status = CASE
                WHEN status = 'done' THEN 'succeeded'
                WHEN status = 'doing' THEN 'needs_reconciliation'
                ELSE 'queued'
            END,
            status = CASE WHEN status = 'doing' THEN 'needs_reconciliation' ELSE status END,
            last_error = CASE
                WHEN status = 'doing'
                THEN 'worker_restart: legacy running task requires reconciliation'
                ELSE last_error
            END
        WHERE request_id IS NULL
        """
    )

    # Pending legacy work is safe to queue. A legacy ``doing`` row may already
    # have external side effects, so it becomes needs_reconciliation instead of
    # being replayed. The inserted RuntimeTask and Task pointer are one statement.
    op.execute(
        sa.text(
            """
            WITH candidates AS (
                SELECT t.*
                FROM tasks AS t
                WHERE t.type::text = 'todo'
                  AND t.active_runtime_task_id IS NULL
                  AND t.status IN ('pending', 'needs_reconciliation')
            ), inserted AS (
                INSERT INTO runtime_tasks (
                    id, task_type, parent_agent_id, child_agent_id, tenant_id,
                    status, prompt, trace_id, depth, root_idempotency_key,
                    config_snapshot_hash, policy_snapshot_hash, metadata_json,
                    created_at, completed_at
                )
                SELECT
                    gen_random_uuid(),
                    'business_task',
                    c.agent_id,
                    c.agent_id,
                    c.tenant_id,
                    CASE WHEN c.status = 'pending' THEN 'pending' ELSE 'needs_reconciliation' END,
                    c.description,
                    'business_task:legacy:' || c.id::text,
                    1,
                    'legacy-business-task:' || c.id::text,
                    md5('legacy-config:' || c.id::text),
                    md5('legacy-policy:' || c.id::text),
                    jsonb_build_object(
                        'schema', 'hive.business_task_run.v1',
                        'business_task_id', c.id::text,
                        'requester_user_id', c.created_by::text,
                        'request_id', c.request_id,
                        'request_hash', c.request_hash,
                        'attempt', 1,
                        'phase', CASE WHEN c.status = 'pending' THEN 'queued' ELSE 'terminal' END,
                        'source', :source,
                        'outcome', CASE
                            WHEN c.status = 'needs_reconciliation'
                            THEN jsonb_build_object(
                                'status', 'needs_reconciliation',
                                'summary', 'Legacy running task requires reconciliation before retry'
                            )
                            ELSE NULL
                        END
                    ),
                    COALESCE(c.created_at, now()),
                    CASE WHEN c.status = 'needs_reconciliation' THEN now() ELSE NULL END
                FROM candidates AS c
                RETURNING id, (metadata_json->>'business_task_id')::uuid AS business_task_id
            )
            UPDATE tasks AS t
            SET active_runtime_task_id = inserted.id,
                execution_attempt = 1,
                updated_at = now()
            FROM inserted
            WHERE t.id = inserted.business_task_id
            """
        ).bindparams(source=BACKFILL_SOURCE)
    )

    op.alter_column("tasks", "request_id", nullable=False)
    op.alter_column("tasks", "request_hash", nullable=False)
    op.create_unique_constraint(
        "uq_tasks_tenant_agent_request_id",
        "tasks",
        ["tenant_id", "agent_id", "request_id"],
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE tasks AS t
            SET active_runtime_task_id = NULL,
                execution_attempt = 0
            FROM runtime_tasks AS rt
            WHERE t.active_runtime_task_id = rt.id
              AND rt.metadata_json->>'source' = :source
            """
        ).bindparams(source=BACKFILL_SOURCE)
    )
    op.execute(
        sa.text("DELETE FROM runtime_tasks WHERE metadata_json->>'source' = :source").bindparams(source=BACKFILL_SOURCE)
    )
    op.drop_constraint("uq_tasks_tenant_agent_request_id", "tasks", type_="unique")
    op.drop_index("ix_tasks_last_execution_status", table_name="tasks")
    op.drop_index("ix_tasks_active_runtime_task_id", table_name="tasks")
    op.drop_constraint("fk_tasks_active_runtime_task_id_runtime_tasks", "tasks", type_="foreignkey")
    for column in (
        "last_result",
        "last_error",
        "last_execution_status",
        "execution_attempt",
        "active_runtime_task_id",
        "request_hash",
        "request_id",
    ):
        op.drop_column("tasks", column)

    op.execute(
        "UPDATE tasks SET status = 'pending' WHERE status IN ('blocked','failed','cancelled','needs_reconciliation')"
    )
    op.drop_constraint("ck_tasks_status", "tasks", type_="check")
    op.execute("ALTER TABLE tasks ALTER COLUMN status DROP DEFAULT")
    op.execute("CREATE TYPE task_status_enum AS ENUM ('pending','doing','done')")
    op.execute("ALTER TABLE tasks ALTER COLUMN status TYPE task_status_enum USING status::task_status_enum")
    op.alter_column("tasks", "status", server_default="pending")
