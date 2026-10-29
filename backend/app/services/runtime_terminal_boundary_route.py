"""Canonical invoker routes and active queue backfill for terminal boundaries."""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

TERMINAL_ROUTE_FUNCTION_BODIES = {
    "set_terminal_boundary_task_route": """
        BEGIN
          SELECT t.task_type INTO NEW.task_type FROM public.runtime_tasks t
          WHERE t.id=NEW.runtime_task_id AND t.tenant_id=NEW.tenant_id;
          RETURN NEW;
        END;
        """,
    "sync_terminal_boundary_task_route": """
        BEGIN
          UPDATE public.runtime_terminal_boundary_outbox q SET task_type=NEW.task_type
          WHERE q.runtime_task_id=NEW.id AND q.tenant_id=NEW.tenant_id
            AND q.status IN ('pending','processing') AND q.task_type IS DISTINCT FROM NEW.task_type;
          RETURN NEW;
        END;
        """,
}

TERMINAL_ROUTE_TRIGGER_DEFINITIONS = {
    "trg_terminal_boundary_task_route": (
        "runtime_terminal_boundary_outbox",
        "CREATE TRIGGER trg_terminal_boundary_task_route BEFORE INSERT OR UPDATE OF runtime_task_id, "
        "tenant_id, task_type, status ON public.runtime_terminal_boundary_outbox FOR EACH ROW "
        "EXECUTE FUNCTION set_terminal_boundary_task_route()",
    ),
    "trg_sync_terminal_boundary_task_route": (
        "runtime_tasks",
        "CREATE TRIGGER trg_sync_terminal_boundary_task_route AFTER UPDATE OF task_type ON public.runtime_tasks "
        "FOR EACH ROW WHEN (((old.task_type)::text IS DISTINCT FROM (new.task_type)::text)) "
        "EXECUTE FUNCTION sync_terminal_boundary_task_route()",
    ),
}


def install_terminal_boundary_task_routes(connection: Connection) -> None:
    if "task_type" not in {
        column["name"] for column in inspect(connection).get_columns("runtime_terminal_boundary_outbox")
    }:
        connection.execute(text("ALTER TABLE runtime_terminal_boundary_outbox ADD COLUMN task_type VARCHAR(50)"))
    for name, body in TERMINAL_ROUTE_FUNCTION_BODIES.items():
        connection.execute(
            text(
                f"CREATE OR REPLACE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql "
                f"SECURITY INVOKER SET search_path=pg_catalog,public AS $${body}$$"
            )
        )
    for name, (table, definition) in TERMINAL_ROUTE_TRIGGER_DEFINITIONS.items():
        connection.execute(text(f"DROP TRIGGER IF EXISTS {name} ON {table}"))
        connection.execute(text(definition.replace("EXECUTE FUNCTION ", "EXECUTE FUNCTION public.")))


def backfill_terminal_boundary_task_routes(connection: Connection) -> None:
    cursor, batches, updated = None, 0, 0
    while True:
        after = "AND q.id>CAST(:after AS uuid)" if cursor is not None else ""
        row = connection.execute(
            text(f"""
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
            """),
            {"after": str(cursor)} if cursor is not None else {},
        ).one()
        if row[0] is None:
            break
        # Advance from the candidate page, even if a row has no same-tenant task.
        cursor, batches, updated = row[0], batches + 1, updated + row[1]
        if batches % 100 == 0:
            logging.getLogger("alembic.runtime.migration").info(
                "Terminal route backfill committed batches=%s rows=%s", batches, updated
            )
    logging.getLogger("alembic.runtime.migration").info(
        "Terminal route backfill complete batches=%s rows=%s", batches, updated
    )
