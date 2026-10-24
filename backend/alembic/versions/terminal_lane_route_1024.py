"""Route active terminal discovery through canonical task-type indexes."""

import logging

from alembic import op
import sqlalchemy as sa

revision = "terminal_lane_route_1024"
down_revision = "terminal_queue_scan_1024"
branch_labels = None
depends_on = None

_INDEXES = {
    "ix_runtime_terminal_boundary_active_lane": (
        "ON runtime_terminal_boundary_outbox (task_type,status,available_at,lease_expires_at) "
        "INCLUDE (tenant_id,runtime_task_id,created_at) WHERE status IN ('pending','processing')"
    ),
    "ix_runtime_tasks_terminal_discovery": (
        "ON runtime_tasks (task_type,terminal_boundary_reconcile_attempted_at,created_at,tenant_id,id) "
        "WHERE terminal_boundary_generation IS NOT NULL AND terminal_boundary_enqueued_at IS NULL"
    ),
}
_BACKFILL_INDEX = "ix_terminal_route_backfill_pending"


def _online_index(name: str, definition: str) -> None:
    invalid = op.get_bind().scalar(sa.text(
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' "
        "AND c.relname=:name AND (NOT i.indisvalid OR NOT i.indisready)"), {"name": name})
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {definition}")


def upgrade() -> None:
    # Fail without queuing a reader-blocking DDL lock behind live traffic.
    op.execute("LOCK TABLE runtime_tasks IN SHARE ROW EXCLUSIVE MODE NOWAIT")
    op.execute("LOCK TABLE runtime_terminal_boundary_outbox IN ACCESS EXCLUSIVE MODE NOWAIT")
    if "task_type" not in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("runtime_terminal_boundary_outbox")}:
        op.add_column("runtime_terminal_boundary_outbox", sa.Column("task_type", sa.String(50), nullable=True))
    op.execute("""
        CREATE OR REPLACE FUNCTION public.set_terminal_boundary_task_route()
        RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
        BEGIN
          SELECT t.task_type INTO NEW.task_type FROM public.runtime_tasks t
          WHERE t.id=NEW.runtime_task_id AND t.tenant_id=NEW.tenant_id;
          RETURN NEW;
        END;
        $$
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_terminal_boundary_task_route ON runtime_terminal_boundary_outbox")
    op.execute("""
        CREATE TRIGGER trg_terminal_boundary_task_route
        BEFORE INSERT OR UPDATE OF runtime_task_id,tenant_id,task_type,status ON runtime_terminal_boundary_outbox
        FOR EACH ROW EXECUTE FUNCTION public.set_terminal_boundary_task_route()
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.sync_terminal_boundary_task_route()
        RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
        BEGIN
          UPDATE public.runtime_terminal_boundary_outbox q SET task_type=NEW.task_type
          WHERE q.runtime_task_id=NEW.id AND q.tenant_id=NEW.tenant_id
            AND q.status IN ('pending','processing') AND q.task_type IS DISTINCT FROM NEW.task_type;
          RETURN NEW;
        END;
        $$
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_sync_terminal_boundary_task_route ON runtime_tasks")
    op.execute("""
        CREATE TRIGGER trg_sync_terminal_boundary_task_route AFTER UPDATE OF task_type ON runtime_tasks
        FOR EACH ROW WHEN (OLD.task_type IS DISTINCT FROM NEW.task_type)
        EXECUTE FUNCTION public.sync_terminal_boundary_task_route()
    """)
    # Commit the short DDL transaction before touching populated queue rows.
    # Each subsequent statement commits independently, including each batch.
    with op.get_context().autocommit_block():
        _online_index(_BACKFILL_INDEX,
            "ON runtime_terminal_boundary_outbox (id) INCLUDE (tenant_id,runtime_task_id) "
            "WHERE task_type IS NULL AND status IN ('pending','processing')")
        cursor, batches, updated = None, 0, 0
        while True:
            after = "AND q.id>CAST(:after AS uuid)" if cursor is not None else ""
            row = op.get_bind().execute(sa.text(f"""
                WITH page AS MATERIALIZED (
                  SELECT q.id,q.tenant_id,q.runtime_task_id FROM runtime_terminal_boundary_outbox q
                  WHERE q.task_type IS NULL AND q.status IN ('pending','processing') {after}
                  ORDER BY q.id LIMIT 1000
                ), updated AS (
                  UPDATE runtime_terminal_boundary_outbox q SET task_type=t.task_type
                  FROM page p CROSS JOIN LATERAL (
                    SELECT t.task_type FROM runtime_tasks t
                    WHERE t.id=p.runtime_task_id AND t.tenant_id=p.tenant_id OFFSET 0
                  ) t WHERE q.id=p.id AND q.tenant_id=p.tenant_id AND q.runtime_task_id=p.runtime_task_id
                    AND q.task_type IS NULL AND q.status IN ('pending','processing')
                  RETURNING q.id
                )
                SELECT (SELECT id FROM page ORDER BY id DESC LIMIT 1), (SELECT count(*) FROM updated)
            """), {"after": str(cursor)} if cursor is not None else {}).one()
            if row[0] is None:
                break
            # Advance from the candidate page, even if a row has no same-tenant task.
            cursor, batches, updated = row[0], batches + 1, updated + row[1]
            if batches % 100 == 0:
                logging.getLogger("alembic.runtime.migration").info(
                    "Terminal route backfill committed batches=%s rows=%s", batches, updated)
        logging.getLogger("alembic.runtime.migration").info(
            "Terminal route backfill complete batches=%s rows=%s", batches, updated)
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_BACKFILL_INDEX}")
        for name, definition in _INDEXES.items():
            _online_index(name, definition)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_BACKFILL_INDEX}")
        for name in reversed(_INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute("DROP TRIGGER IF EXISTS trg_sync_terminal_boundary_task_route ON runtime_tasks")
    op.execute("DROP FUNCTION IF EXISTS public.sync_terminal_boundary_task_route()")
    op.execute("DROP TRIGGER IF EXISTS trg_terminal_boundary_task_route ON runtime_terminal_boundary_outbox")
    op.execute("DROP FUNCTION IF EXISTS public.set_terminal_boundary_task_route()")
    op.drop_column("runtime_terminal_boundary_outbox", "task_type")
