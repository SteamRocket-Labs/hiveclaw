"""The legacy audit schema must retain full external execution identities."""

import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text


async def test_populated_external_identity_upgrade_and_rollback_preserve_evidence(owner_sessionmaker):
    path = Path(__file__).resolve().parents[2] / "alembic/versions/external_execution_identity_1018.py"
    spec = importlib.util.spec_from_file_location("external_identity_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = "identity_upgrade_" + uuid4().hex

    def check(connection):
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        for table in ("security_audit_events", "invocation_spans"):
            connection.execute(text(f"CREATE TABLE {table} (id integer PRIMARY KEY, execution_identity_type varchar(20), evidence text)"))
            connection.execute(text(f"INSERT INTO {table} VALUES (1, 'delegated_user', 'original evidence'), (2, NULL, 'legacy evidence')"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        for table in ("security_audit_events", "invocation_spans"):
            rows = connection.execute(text(f"SELECT execution_identity_type, evidence FROM {table} ORDER BY id")).all()
            assert rows == [("delegated_user", "original evidence"), (None, "legacy evidence")]
            connection.execute(text(f"INSERT INTO {table} VALUES (3, 'external_principal_bound', 'verified channel evidence')"))
        migration.downgrade()
        for table in ("security_audit_events", "invocation_spans"):
            assert connection.scalar(text(f"SELECT execution_identity_type FROM {table} WHERE id=3")) == "external_principal_bound"
            assert connection.scalar(text("SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema=:schema AND table_name=:table AND column_name='execution_identity_type'"),
                {"schema": schema, "table": table}) == 40

    async with owner_sessionmaker() as db:
        await (await db.connection()).run_sync(check)
        await db.rollback()
