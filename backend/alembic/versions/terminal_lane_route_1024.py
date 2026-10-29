"""Route active terminal discovery through canonical task-type indexes."""

from alembic import op
import sqlalchemy as sa

from app.services.runtime_terminal_boundary_route import (
    TERMINAL_ROUTE_FUNCTION_BODIES as _FUNCTION_BODIES,
    TERMINAL_ROUTE_TRIGGER_DEFINITIONS as _TRIGGER_DEFINITIONS,
    backfill_terminal_boundary_task_routes,
    install_terminal_boundary_task_routes,
)

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
    invalid = op.get_bind().scalar(
        sa.text(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' "
            "AND c.relname=:name AND (NOT i.indisvalid OR NOT i.indisready)"
        ),
        {"name": name},
    )
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {definition}")


def _routes_installed() -> bool:
    column = next(
        (
            c
            for c in sa.inspect(op.get_bind()).get_columns("runtime_terminal_boundary_outbox")
            if c["name"] == "task_type"
        ),
        None,
    )
    if not column or not isinstance(column["type"], sa.String) or column["type"].length != 50 or not column["nullable"]:
        return False
    functions = (
        op.get_bind()
        .execute(
            sa.text("""
        SELECT p.proname,p.prosrc,p.prosecdef,p.proconfig FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname='public' AND p.pronargs=0 AND p.prorettype='trigger'::regtype AND l.lanname='plpgsql'
          AND p.proname IN ('set_terminal_boundary_task_route','sync_terminal_boundary_task_route')
    """)
        )
        .all()
    )
    if len(functions) != len(_FUNCTION_BODIES) or any(
        row.prosecdef
        or row.prosrc.strip() != _FUNCTION_BODIES[row.proname].strip()
        or [value.replace(" ", "") for value in (row.proconfig or [])] != ["search_path=pg_catalog,public"]
        for row in functions
    ):
        return False
    triggers = (
        op.get_bind()
        .execute(
            sa.text("""
        SELECT t.tgname,t.tgenabled::text AS enabled,pg_get_triggerdef(t.oid) AS definition
        FROM pg_trigger t WHERE t.tgrelid IN ('public.runtime_tasks'::regclass,
          'public.runtime_terminal_boundary_outbox'::regclass)
          AND t.tgname IN ('trg_terminal_boundary_task_route','trg_sync_terminal_boundary_task_route')
    """)
        )
        .all()
    )
    return len(triggers) == len(_TRIGGER_DEFINITIONS) and all(
        row.enabled == "O" and row.definition == _TRIGGER_DEFINITIONS[row.tgname][1] for row in triggers
    )


def _install_routes() -> None:
    # Fail without queuing a reader-blocking DDL lock behind live traffic.
    op.execute("LOCK TABLE runtime_tasks IN SHARE ROW EXCLUSIVE MODE NOWAIT")
    op.execute("LOCK TABLE runtime_terminal_boundary_outbox IN ACCESS EXCLUSIVE MODE NOWAIT")
    install_terminal_boundary_task_routes(op.get_bind())


def upgrade() -> None:
    # Reuse only the exact installed invoker routes. Live readers must not force
    # an interrupted data backfill to acquire the same DDL locks again.
    if not _routes_installed():
        _install_routes()
    # Commit the short DDL transaction before touching populated queue rows.
    # Each subsequent statement commits independently, including each batch.
    with op.get_context().autocommit_block():
        _online_index(
            _BACKFILL_INDEX,
            "ON runtime_terminal_boundary_outbox (id) INCLUDE (tenant_id,runtime_task_id) "
            "WHERE task_type IS NULL AND status IN ('pending','processing')",
        )
        backfill_terminal_boundary_task_routes(op.get_bind())
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
