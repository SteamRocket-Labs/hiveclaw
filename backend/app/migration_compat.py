"""Fail-closed helpers for reconciling startup-created schema during Alembic upgrades.

Historical Hive runtimes called ``metadata.create_all()`` on versioned databases.
That operation never alters an existing table, but it can create tables owned by
future Alembic revisions.  These helpers let those revisions adopt a compatible
bootstrap object without skipping their data backfills, RLS, triggers, or version
advance.  A shape mismatch raises instead of silently accepting unknown schema.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from alembic.operations import Operations
import sqlalchemy as sa


class SchemaDriftError(RuntimeError):
    """Existing schema cannot safely satisfy the migration contract."""


def _inspector(operations: Operations) -> sa.Inspector | None:
    # Always create a fresh inspector: PostgreSQL transactional DDL performed by
    # the previous helper call must be visible to the next compatibility probe.
    get_bind = getattr(operations, "get_bind", None)
    if get_bind is None:
        return None
    return sa.inspect(get_bind())


def create_table_if_missing(
    operations: Operations,
    table_name: str,
    *elements: Any,
    **kwargs: Any,
) -> bool:
    """Create a table, or adopt it when all migration-declared columns exist."""

    inspector = _inspector(operations)
    if inspector is None or not inspector.has_table(table_name, schema=kwargs.get("schema")):
        operations.create_table(table_name, *elements, **kwargs)
        return True

    actual_columns = {item["name"] for item in inspector.get_columns(table_name, schema=kwargs.get("schema"))}
    expected_columns = {
        element.name for element in elements if isinstance(element, sa.Column) and element.name is not None
    }
    missing_columns = sorted(expected_columns - actual_columns)
    if missing_columns:
        raise SchemaDriftError(
            f"existing table {table_name!r} is missing migration columns: {', '.join(missing_columns)}"
        )
    return False


def create_index_if_missing(
    operations: Operations,
    index_name: str,
    table_name: str,
    columns: Sequence[Any],
    **kwargs: Any,
) -> bool:
    """Create an index, or verify a same-named index targets the same columns."""

    inspector = _inspector(operations)
    if inspector is None:
        operations.create_index(index_name, table_name, columns, **kwargs)
        return True
    indexes = {item["name"]: item for item in inspector.get_indexes(table_name, schema=kwargs.get("schema"))}
    existing = indexes.get(index_name)
    if existing is None:
        operations.create_index(index_name, table_name, columns, **kwargs)
        return True

    expected_columns = list(columns) if all(isinstance(column, str) for column in columns) else None
    actual_columns = existing.get("column_names")
    if expected_columns is not None and actual_columns != expected_columns:
        raise SchemaDriftError(
            f"existing index {index_name!r} has columns {actual_columns!r}; expected {expected_columns!r}"
        )
    expected_unique = bool(kwargs.get("unique", False))
    if bool(existing.get("unique", False)) != expected_unique:
        raise SchemaDriftError(
            f"existing index {index_name!r} unique={existing.get('unique')!r}; expected unique={expected_unique!r}"
        )
    return False


def add_column_if_missing(
    operations: Operations,
    table_name: str,
    column: sa.Column[Any],
    **kwargs: Any,
) -> bool:
    """Add a column unless current metadata already created it."""

    inspector = _inspector(operations)
    if inspector is None:
        operations.add_column(table_name, column, **kwargs)
        return True
    columns = {item["name"] for item in inspector.get_columns(table_name, schema=kwargs.get("schema"))}
    if column.name in columns:
        return False
    operations.add_column(table_name, column, **kwargs)
    return True


def create_foreign_key_if_missing(
    operations: Operations,
    constraint_name: str,
    source_table: str,
    referent_table: str,
    local_cols: Sequence[str],
    remote_cols: Sequence[str],
    **kwargs: Any,
) -> bool:
    """Create a named foreign key, or fail if its existing shape differs."""

    inspector = _inspector(operations)
    if inspector is None:
        operations.create_foreign_key(
            constraint_name,
            source_table,
            referent_table,
            local_cols,
            remote_cols,
            **kwargs,
        )
        return True
    constraints = {
        item["name"]: item
        for item in inspector.get_foreign_keys(
            source_table,
            schema=kwargs.get("source_schema"),
        )
        if item.get("name")
    }
    existing = constraints.get(constraint_name)
    if existing is None:
        operations.create_foreign_key(
            constraint_name,
            source_table,
            referent_table,
            local_cols,
            remote_cols,
            **kwargs,
        )
        return True

    expected = (list(local_cols), referent_table, list(remote_cols))
    actual = (
        existing.get("constrained_columns"),
        existing.get("referred_table"),
        existing.get("referred_columns"),
    )
    if actual != expected:
        raise SchemaDriftError(f"existing foreign key {constraint_name!r} has shape {actual!r}; expected {expected!r}")
    return False


def create_unique_constraint_if_missing(
    operations: Operations,
    constraint_name: str,
    table_name: str,
    columns: Sequence[str],
    **kwargs: Any,
) -> bool:
    """Create a named unique constraint, or verify its existing columns."""

    inspector = _inspector(operations)
    if inspector is None:
        operations.create_unique_constraint(constraint_name, table_name, columns, **kwargs)
        return True
    constraints = {
        item["name"]: item
        for item in inspector.get_unique_constraints(
            table_name,
            schema=kwargs.get("schema"),
        )
        if item.get("name")
    }
    existing = constraints.get(constraint_name)
    if existing is None:
        operations.create_unique_constraint(constraint_name, table_name, columns, **kwargs)
        return True
    if existing.get("column_names") != list(columns):
        raise SchemaDriftError(
            f"existing unique constraint {constraint_name!r} has columns "
            f"{existing.get('column_names')!r}; expected {list(columns)!r}"
        )
    return False
