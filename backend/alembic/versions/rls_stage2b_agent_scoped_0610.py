"""Stage-2b: add tenant_id column + index + RLS policy to 18 agent-scoped tables.

These tables carry ``agent_id`` (or ``parent_agent_id`` / ``task_id``) but had NO
``tenant_id`` column and NO RLS policy. Under the stage-3 non-owner role flip they
would stay fully readable cross-tenant — the exact silent isolation leak stage-2a
closed for the 21 tenant_id-carrying tables, except here the column does not even
exist yet.

What this migration does (per table):
* ``ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id)`` — **NULLABLE**,
  matching every existing tenant_id column (``agents.tenant_id`` et al. are all
  ``Mapped[uuid.UUID | None]``, pairing with the policy's ``OR tenant_id IS NULL``
  global-row escape). Adding a nullable column with an FK does not rewrite the
  table or scan existing rows (they are all NULL), so it is online-safe.
* a ``tenant_id`` index named to match SQLAlchemy's ``index=True`` default
  (``ix_<table>_tenant_id``) so create_all on a fresh DB and this migration agree.
* ``ENABLE ROW LEVEL SECURITY`` + the standard ``tenant_isolation_<table>`` policy
  (USING-only, identical to add_row_level_security / stage-2a). ENABLE — not FORCE
  — so it is inert under the production owner connection and only bites at the
  stage-3 role flip.

Deliberately NOT here (gated, separate steps — see docs/rls-enforcement-migration-plan.md):
* **Backfill** of tenant_id is an irreversible data step → ``scripts/backfill_stage2b_tenant_id.py``
  (dry-run + owner confirmation). It runs under the owner connection BEFORE the role
  flip, so the ENABLE policy does not block it.
* **NOT NULL** is intentionally never set — nullable matches the codebase convention
  and the policy's global-row escape. Isolation safety comes from: backfill
  exhaustion (zero NULL residue on agent_id-NOT-NULL tables), accessors setting
  tenant_id explicitly on INSERT (the policy has USING only, NO WITH CHECK, so a
  NULL row stays globally visible), and the stage-0 shadow exhaustion before flip.

Revision ID: rls_stage2b_agent_scoped_0610
Revises: rls_stage2a_tenant_policies_0610
Create Date: 2026-06-10
"""

from typing import Sequence, Union

from alembic import op

revision: str = "rls_stage2b_agent_scoped_0610"
down_revision: Union[str, None] = "rls_stage2a_tenant_policies_0610"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 18 agent-scoped tables (agent_id / parent_agent_id / task_id, no tenant_id).
# Mirrors db_bootstrap.RLS_TENANT_TABLES stage-2b block — keep in sync.
_STAGE2B_TABLES = (
    "agent_activity_logs",
    "agent_agent_relationships",
    "agent_capability_installs",
    "agent_permissions",
    "agent_relationships",
    "agent_schedules",
    "agent_tools",
    "agent_triggers",
    "approval_requests",
    "audit_logs",
    "channel_configs",
    "chat_messages",
    "chat_sessions",
    "gateway_messages",
    "pending_reply_contexts",
    "runtime_tasks",
    "task_logs",
    "tasks",
)


def _table_exists(table: str) -> bool:
    from sqlalchemy import text

    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :table"),
        {"table": table},
    )
    return result.scalar() is not None


def upgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', '', false)")
    for table in _STAGE2B_TABLES:
        if not _table_exists(table):
            # Created by create_all/bootstrap on a not-yet-started deployment;
            # bootstrap's apply_rls_policies + ORM column cover it when it appears.
            continue
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id)")
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table} (tenant_id)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            DO $$ BEGIN
                CREATE POLICY tenant_isolation_{table} ON {table}
                    USING (
                        current_setting('app.current_tenant_id', true) = 'BYPASS'
                        OR tenant_id::text = current_setting('app.current_tenant_id', true)
                        OR tenant_id IS NULL
                    );
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$
        """)


def downgrade() -> None:
    for table in _STAGE2B_TABLES:
        if not _table_exists(table):
            continue
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_tenant_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS tenant_id")
