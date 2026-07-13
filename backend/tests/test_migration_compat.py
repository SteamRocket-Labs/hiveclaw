from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def _operations(connection) -> Operations:
    return Operations(MigrationContext.configure(connection))


def test_create_table_if_missing_reuses_compatible_bootstrap_table() -> None:
    from app.migration_compat import create_table_if_missing

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE receipts (id INTEGER PRIMARY KEY, payload TEXT, later_column TEXT)"))
        created = create_table_if_missing(
            _operations(connection),
            "receipts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("payload", sa.Text(), nullable=True),
        )

    assert created is False


def test_create_table_if_missing_rejects_incompatible_bootstrap_table() -> None:
    from app.migration_compat import SchemaDriftError, create_table_if_missing

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE receipts (id INTEGER PRIMARY KEY)"))
        with pytest.raises(SchemaDriftError, match="payload"):
            create_table_if_missing(
                _operations(connection),
                "receipts",
                sa.Column("id", sa.Integer(), primary_key=True),
                sa.Column("payload", sa.Text(), nullable=True),
            )


def test_create_table_and_index_helpers_are_replay_safe() -> None:
    from app.migration_compat import create_index_if_missing, create_table_if_missing

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        operations = _operations(connection)
        assert create_table_if_missing(
            operations,
            "receipts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("payload", sa.Text(), nullable=True),
        )
        assert create_index_if_missing(operations, "ix_receipts_payload", "receipts", ["payload"])
        assert create_index_if_missing(operations, "ix_receipts_payload", "receipts", ["payload"]) is False

        index_names = {item["name"] for item in inspect(connection).get_indexes("receipts")}

    assert index_names == {"ix_receipts_payload"}


def test_add_column_if_missing_is_replay_safe() -> None:
    from app.migration_compat import add_column_if_missing

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        operations = _operations(connection)
        operations.create_table("receipts", sa.Column("id", sa.Integer(), primary_key=True))
        assert add_column_if_missing(operations, "receipts", sa.Column("payload", sa.Text(), nullable=True))
        assert add_column_if_missing(operations, "receipts", sa.Column("payload", sa.Text(), nullable=True)) is False

        columns = {item["name"] for item in inspect(connection).get_columns("receipts")}

    assert columns == {"id", "payload"}
