"""Add the durable caller-owned runtime terminal boundary outbox.

Revision ID: runtime_terminal_boundary_0831
Revises: a2a_continuation_task_0828
Create Date: 2026-08-31
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "runtime_terminal_boundary_0831"
down_revision = "a2a_continuation_task_0828"
branch_labels = None
depends_on = None


_TABLE = "runtime_terminal_boundary_outbox"
_TASK_PENDING_INDEX = "ix_runtime_tasks_terminal_boundary_pending"
_GENERATION_FUNCTION = "set_runtime_task_terminal_boundary_generation"
_GENERATION_TRIGGER = "trg_runtime_task_terminal_boundary_generation"
_TERMINAL_TASK_TYPES = (
    "'web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan', "
    "'a2a_continuation', 'business_task', 'trigger', 'delegation'"
)
_TERMINAL_STATUSES = "'completed', 'failed', 'killed', 'skipped', 'needs_reconciliation'"
_TASK_PENDING_PREDICATE = (
    "terminal_boundary_generation IS NOT NULL "
    "AND terminal_boundary_enqueued_at IS NULL "
    f"AND task_type IN ({_TERMINAL_TASK_TYPES}) "
    f"AND status IN ({_TERMINAL_STATUSES}) "
    "AND tenant_id IS NOT NULL"
)


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "runtime_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(length=200), nullable=False),
        sa.Column("event_kind", sa.String(length=80), nullable=False),
        sa.Column("terminal_status", sa.String(length=40), nullable=False),
        sa.Column("authority_ref", sa.String(length=100), nullable=False),
        sa.Column("authority_id", sa.String(length=200), nullable=False),
        sa.Column("binding_json", postgresql.JSONB(), nullable=False),
        sa.Column("binding_sha256", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("claimed_by", sa.String(length=200), nullable=True),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivery_receipt_json", postgresql.JSONB(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','processing','delivered','dead_letter')",
            name="ck_runtime_terminal_boundary_outbox_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_runtime_terminal_boundary_outbox_attempt_count",
        ),
        sa.CheckConstraint(
            "char_length(binding_sha256) = 64",
            name="ck_runtime_terminal_boundary_outbox_binding_sha",
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) = 64",
            name="ck_runtime_terminal_boundary_outbox_idempotency_sha",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "event_kind",
            "authority_ref",
            "authority_id",
            name="uq_runtime_terminal_boundary_authority_event",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_runtime_terminal_boundary_idempotency",
        ),
    )
    op.create_index(
        "ix_runtime_terminal_boundary_outbox_tenant_id",
        _TABLE,
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_runtime_terminal_boundary_claim",
        _TABLE,
        ["tenant_id", "status", "available_at", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_runtime_terminal_boundary_task",
        _TABLE,
        ["tenant_id", "runtime_task_id"],
        unique=False,
    )

    task_columns = _columns("runtime_tasks")
    if "terminal_boundary_generation" not in task_columns:
        op.add_column(
            "runtime_tasks",
            sa.Column("terminal_boundary_generation", sa.SmallInteger(), nullable=True),
        )
    if "terminal_boundary_enqueued_at" not in task_columns:
        op.add_column(
            "runtime_tasks",
            sa.Column("terminal_boundary_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "terminal_boundary_reconcile_attempted_at" not in task_columns:
        op.add_column(
            "runtime_tasks",
            sa.Column("terminal_boundary_reconcile_attempted_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "terminal_boundary_reconcile_attempt_count" not in task_columns:
        op.add_column(
            "runtime_tasks",
            sa.Column(
                "terminal_boundary_reconcile_attempt_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
        )
    if "terminal_boundary_reconcile_last_error" not in task_columns:
        op.add_column(
            "runtime_tasks",
            sa.Column("terminal_boundary_reconcile_last_error", sa.String(length=100), nullable=True),
        )

    # Historical terminal rows must never be replayed as new semantic events.
    # Only active work with a live consumer is adopted. A trigger normalizes
    # future raw/rolling-writer inserts because a scalar server default cannot
    # depend on ``task_type``. Drop any pre-release scalar default so a missing
    # guard fails closed instead of admitting unsupported task types.
    op.alter_column(
        "runtime_tasks",
        "terminal_boundary_generation",
        existing_type=sa.SmallInteger(),
        server_default=None,
    )
    op.execute(
        sa.text(
            "UPDATE runtime_tasks SET terminal_boundary_generation=1 "
            "WHERE terminal_boundary_generation IS NULL "
            f"AND task_type IN ({_TERMINAL_TASK_TYPES}) "
            "AND status IN ('pending', 'running', 'resumable', 'suspended')"
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {_GENERATION_FUNCTION}()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
              IF NEW.task_type IN ({_TERMINAL_TASK_TYPES}) THEN
                IF NEW.terminal_boundary_generation IS NULL THEN
                  NEW.terminal_boundary_generation := 1;
                END IF;
              ELSE
                NEW.terminal_boundary_generation := NULL;
              END IF;
              RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_GENERATION_TRIGGER}
            BEFORE INSERT OR UPDATE OF task_type, terminal_boundary_generation
            ON runtime_tasks
            FOR EACH ROW
            EXECUTE FUNCTION {_GENERATION_FUNCTION}()
            """
        )
    )
    op.create_index(
        _TASK_PENDING_INDEX,
        "runtime_tasks",
        [
            sa.text("(terminal_boundary_reconcile_attempted_at IS NOT NULL)"),
            sa.text("terminal_boundary_reconcile_attempted_at ASC"),
            sa.text("created_at ASC"),
        ],
        unique=False,
        postgresql_where=sa.text(_TASK_PENDING_PREDICATE),
    )

    chat_session_columns = _columns("chat_sessions")
    if "summary_through_sequence" not in chat_session_columns:
        op.add_column(
            "chat_sessions",
            sa.Column("summary_through_sequence", sa.BigInteger(), nullable=True),
        )

    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON {_TABLE}")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{_TABLE} ON {_TABLE}
        USING (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        WITH CHECK (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        """
    )


def downgrade() -> None:
    chat_session_columns = _columns("chat_sessions")
    if "summary_through_sequence" in chat_session_columns:
        op.drop_column("chat_sessions", "summary_through_sequence")
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_GENERATION_TRIGGER} ON runtime_tasks"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_GENERATION_FUNCTION}()"))
    op.drop_index(_TASK_PENDING_INDEX, table_name="runtime_tasks")
    task_columns = _columns("runtime_tasks")
    for column in (
        "terminal_boundary_reconcile_last_error",
        "terminal_boundary_reconcile_attempt_count",
        "terminal_boundary_reconcile_attempted_at",
        "terminal_boundary_enqueued_at",
        "terminal_boundary_generation",
    ):
        if column in task_columns:
            op.drop_column("runtime_tasks", column)
    op.drop_table("runtime_terminal_boundary_outbox")
