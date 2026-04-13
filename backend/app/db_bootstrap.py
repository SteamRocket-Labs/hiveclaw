"""Helpers for bootstrapping an unversioned database to the current schema."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.sql.schema import MetaData

from app.db_legacy_gateway_conversation_migration import promote_legacy_gateway_conversations
from app.db_legacy_schedule_migration import promote_legacy_schedules_to_triggers

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
    promote_legacy_schedules_to_triggers(connection)
    promote_legacy_gateway_conversations(connection)
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
