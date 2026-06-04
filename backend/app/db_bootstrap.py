"""Helpers for bootstrapping an unversioned database to the current schema."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.sql.schema import MetaData

_ALEMBIC_VERSION_TABLE = "alembic_version"
_CORE_APP_TABLES = {
    "users",
    "agents",
    "tenants",
    "chat_messages",
    "chat_sessions",
    "tasks",
    "llm_models",
}

# Tenant tables that must carry RLS from day one. Mirrors
# alembic/versions/add_row_level_security.py (_TENANT_TABLES) — that historic
# migration is immutable, so the bootstrap path keeps its own copy; keep the
# two lists in sync when adding tenant tables.
RLS_TENANT_TABLES: tuple[str, ...] = (
    "agents",
    "users",
    "llm_models",
    "skills",
    "tools",
    "plaza_posts",
    "org_departments",
    "org_members",
    "config_revisions",
)

# Tables that additionally get FORCE ROW LEVEL SECURITY: the production
# connection IS the table owner (P0 gap B), so ENABLE alone is inert there.
# New table families start here; legacy tables move over only after every
# accessor is wired through tenant_scoped_session. Mirrors
# alembic/versions/add_workflow_tables_0604.py.
RLS_FORCED_TENANT_TABLES: tuple[str, ...] = (
    "workflow_definitions",
    "workflow_steps",
    "workflow_leaf_calls",
    "workflow_quotas",
    "coordination_leases",
    "coordination_signals",
    "coordination_checkpoints",
)


def apply_rls_policies(
    connection: Connection,
    tables: Sequence[str] = RLS_TENANT_TABLES,
    forced_tables: Sequence[str] = RLS_FORCED_TENANT_TABLES,
) -> None:
    """Enable RLS + tenant policy on every existing table in ``tables`` /
    ``forced_tables`` (the latter also get FORCE ROW LEVEL SECURITY).

    §9 P0 gap fix: ``bootstrap_database_to_head`` used to create the schema
    via ``metadata.create_all`` and stamp head WITHOUT ever running the
    ``add_row_level_security`` migration — every fresh deployment shipped
    with zero RLS policies. The policy shape below matches that migration
    exactly. Idempotent (duplicate policies are swallowed); tables that don't
    exist in this database are skipped.
    """
    if connection.dialect.name != "postgresql":  # RLS is a PostgreSQL feature
        return
    existing = set(inspect(connection).get_table_names())
    connection.execute(text("SELECT set_config('app.current_tenant_id', '', false)"))
    for table, forced in [(t, False) for t in tables] + [(t, True) for t in forced_tables]:
        if table not in existing:
            continue
        connection.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        if forced:
            connection.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        connection.execute(
            text(
                f"""
                DO $$ BEGIN
                    CREATE POLICY tenant_isolation_{table} ON {table}
                        USING (
                            current_setting('app.current_tenant_id', true) = 'BYPASS'
                            OR tenant_id::text = current_setting('app.current_tenant_id', true)
                            OR tenant_id IS NULL
                        );
                EXCEPTION WHEN duplicate_object THEN NULL;
                END $$
                """
            )
        )


class AlembicContextProtocol(Protocol):
    def configure(self, **kwargs) -> None: ...

    def begin_transaction(self): ...

    def run_migrations(self) -> None: ...


def should_bootstrap_database(connection: Connection) -> bool:
    """Return True when the database should be initialized from current metadata.

    We bootstrap in two cases:
    1. Truly empty database
    2. Database with app tables but without Alembic version tracking
    """
    tables = set(inspect(connection).get_table_names())
    if _ALEMBIC_VERSION_TABLE in tables:
        return False
    if not tables:
        return True
    return bool(tables & _CORE_APP_TABLES)


def bootstrap_database_to_head(connection: Connection, metadata: MetaData, heads: Sequence[str]) -> None:
    """Create the current schema and stamp Alembic heads into an unversioned DB."""
    metadata.create_all(bind=connection)
    # Stamping head skips every migration — including add_row_level_security.
    # Apply the RLS policies explicitly so fresh deployments are not born
    # without tenant isolation (§9 P0 gap fix).
    apply_rls_policies(connection)
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS alembic_version (
                version_num VARCHAR(32) NOT NULL PRIMARY KEY
            )
            """
        )
    )
    connection.execute(text("DELETE FROM alembic_version"))
    for head in heads:
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:version_num)"),
            {"version_num": head},
        )


def run_migrations_with_bootstrap(
    connection: Connection,
    *,
    alembic_context: AlembicContextProtocol,
    metadata: MetaData,
    heads: Sequence[str],
    should_bootstrap_fn=should_bootstrap_database,
    bootstrap_fn=bootstrap_database_to_head,
) -> None:
    """Either bootstrap an unversioned DB or run the normal Alembic path."""
    had_transaction = connection.in_transaction()
    if should_bootstrap_fn(connection):
        bootstrap_fn(connection, metadata, heads)
        if connection.in_transaction() and not had_transaction:
            connection.commit()
        return

    alembic_context.configure(connection=connection, target_metadata=metadata)
    with alembic_context.begin_transaction():
        alembic_context.run_migrations()
    if connection.in_transaction() and not had_transaction:
        connection.commit()
