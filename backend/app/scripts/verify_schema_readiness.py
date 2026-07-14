"""Fail-closed post-migration catalog verification.

This command reads only mechanical database facts: Alembic revision heads,
table presence, RLS flags, policy presence, and strict tenant-column nullability.
It never inspects row content or derives product semantics.

Run with the schema-owner connection after migrations and before application
traffic is accepted::

    python -m app.scripts.verify_schema_readiness
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.database import Base, schema_engine
from app.db_bootstrap import RLS_FORCED_TENANT_TABLES, STRICT_TENANT_RLS_TABLES
from app.models import import_all_models


BACKEND_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class SchemaTableState:
    table_name: str
    exists: bool
    rls_enabled: bool
    rls_forced: bool
    policy_count: int
    has_tenant_id: bool
    tenant_id_not_null: bool


@dataclass(frozen=True, slots=True)
class SchemaReadinessIssue:
    code: str
    object_name: str
    reason: str
    retryable: bool = True


@dataclass(frozen=True, slots=True)
class SchemaReadinessReport:
    expected_heads: tuple[str, ...]
    actual_heads: tuple[str, ...]
    checked_table_count: int
    issues: tuple[SchemaReadinessIssue, ...]

    @property
    def ready(self) -> bool:
        return not self.issues


def evaluate_schema_readiness(
    *,
    expected_heads: Sequence[str],
    actual_heads: Sequence[str],
    expected_rls_tables: Sequence[str],
    strict_tenant_tables: Sequence[str],
    table_states: Mapping[str, SchemaTableState],
    required_rls_tables: Sequence[str] | None = None,
) -> SchemaReadinessReport:
    """Evaluate exact catalog invariants without inspecting semantic content."""

    normalized_expected_heads = tuple(sorted(set(expected_heads)))
    normalized_actual_heads = tuple(sorted(set(actual_heads)))
    expected_tables = tuple(dict.fromkeys(expected_rls_tables))
    strict_tables = set(strict_tenant_tables)
    required_tables = set(expected_tables if required_rls_tables is None else required_rls_tables)
    issues: list[SchemaReadinessIssue] = []

    if normalized_actual_heads != normalized_expected_heads:
        issues.append(
            SchemaReadinessIssue(
                code="alembic_head_mismatch",
                object_name="alembic_version",
                reason=(
                    f"expected={list(normalized_expected_heads)!r} "
                    f"actual={list(normalized_actual_heads)!r}"
                ),
            )
        )

    for table_name in expected_tables:
        state = table_states.get(table_name)
        if state is None or not state.exists:
            if table_name in required_tables:
                issues.append(
                    SchemaReadinessIssue(
                        code="schema_table_missing",
                        object_name=table_name,
                        reason="live model table is absent from public schema",
                    )
                )
            continue
        if not state.rls_enabled:
            issues.append(
                SchemaReadinessIssue(
                    code="rls_not_enabled",
                    object_name=table_name,
                    reason="pg_class.relrowsecurity is false",
                )
            )
        if not state.rls_forced:
            issues.append(
                SchemaReadinessIssue(
                    code="rls_not_forced",
                    object_name=table_name,
                    reason="pg_class.relforcerowsecurity is false",
                )
            )
        if state.policy_count < 1:
            issues.append(
                SchemaReadinessIssue(
                    code="rls_policy_missing",
                    object_name=table_name,
                    reason="pg_policy has no policy for expected RLS table",
                )
            )
        if table_name in strict_tables:
            if not state.has_tenant_id:
                issues.append(
                    SchemaReadinessIssue(
                        code="strict_tenant_column_missing",
                        object_name=table_name,
                        reason="strict tenant table has no tenant_id column",
                    )
                )
            elif not state.tenant_id_not_null:
                issues.append(
                    SchemaReadinessIssue(
                        code="strict_tenant_column_nullable",
                        object_name=table_name,
                        reason="strict tenant_id column is not marked NOT NULL",
                    )
                )

    return SchemaReadinessReport(
        expected_heads=normalized_expected_heads,
        actual_heads=normalized_actual_heads,
        checked_table_count=len(expected_tables),
        issues=tuple(issues),
    )


def expected_alembic_heads() -> tuple[str, ...]:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return tuple(sorted(ScriptDirectory.from_config(config).get_heads()))


_CATALOG_SQL = text(
    """
    WITH expected(table_name) AS (
        SELECT unnest(CAST(:expected_tables AS text[]))
    )
    SELECT
        expected.table_name,
        catalog.oid IS NOT NULL AS exists,
        COALESCE(catalog.relrowsecurity, false) AS rls_enabled,
        COALESCE(catalog.relforcerowsecurity, false) AS rls_forced,
        count(DISTINCT policy.oid)::integer AS policy_count,
        COALESCE(bool_or(attribute.attname = 'tenant_id'), false) AS has_tenant_id,
        COALESCE(
            bool_or(attribute.attname = 'tenant_id' AND attribute.attnotnull),
            false
        ) AS tenant_id_not_null
    FROM expected
    LEFT JOIN pg_namespace namespace ON namespace.nspname = 'public'
    LEFT JOIN pg_class catalog
        ON catalog.relnamespace = namespace.oid
       AND catalog.relname = expected.table_name
       AND catalog.relkind = 'r'
    LEFT JOIN pg_policy policy ON policy.polrelid = catalog.oid
    LEFT JOIN pg_attribute attribute
        ON attribute.attrelid = catalog.oid
       AND attribute.attnum > 0
       AND NOT attribute.attisdropped
    GROUP BY
        expected.table_name,
        catalog.oid,
        catalog.relrowsecurity,
        catalog.relforcerowsecurity
    ORDER BY expected.table_name
    """
)


async def inspect_schema_readiness(connection: AsyncConnection) -> SchemaReadinessReport:
    import_all_models()
    expected_heads = expected_alembic_heads()
    actual_heads = tuple(
        str(row.version_num)
        for row in (await connection.execute(text("SELECT version_num FROM alembic_version"))).all()
    )
    rows = (
        await connection.execute(
            _CATALOG_SQL,
            {"expected_tables": list(RLS_FORCED_TENANT_TABLES)},
        )
    ).all()
    table_states = {
        str(row.table_name): SchemaTableState(
            table_name=str(row.table_name),
            exists=bool(row.exists),
            rls_enabled=bool(row.rls_enabled),
            rls_forced=bool(row.rls_forced),
            policy_count=int(row.policy_count or 0),
            has_tenant_id=bool(row.has_tenant_id),
            tenant_id_not_null=bool(row.tenant_id_not_null),
        )
        for row in rows
    }
    return evaluate_schema_readiness(
        expected_heads=expected_heads,
        actual_heads=actual_heads,
        expected_rls_tables=RLS_FORCED_TENANT_TABLES,
        strict_tenant_tables=STRICT_TENANT_RLS_TABLES,
        table_states=table_states,
        required_rls_tables=tuple(
            table_name for table_name in RLS_FORCED_TENANT_TABLES if table_name in Base.metadata.tables
        ),
    )


def _report_payload(report: SchemaReadinessReport) -> dict[str, object]:
    return {
        "ready": report.ready,
        "expected_heads": list(report.expected_heads),
        "actual_heads": list(report.actual_heads),
        "checked_table_count": report.checked_table_count,
        "issues": [asdict(issue) for issue in report.issues],
    }


async def _amain() -> int:
    try:
        async with schema_engine.connect() as connection:
            report = await inspect_schema_readiness(connection)
    except Exception as exc:  # noqa: BLE001 - boundary converts infrastructure failures to a typed exit.
        payload = {
            "ready": False,
            "issues": [
                {
                    "code": "schema_readiness_inspection_failed",
                    "object_name": "database",
                    "reason": type(exc).__name__,
                    "retryable": True,
                }
            ],
        }
        print("[schema-readiness] " + json.dumps(payload, sort_keys=True))
        return 3

    print("[schema-readiness] " + json.dumps(_report_payload(report), sort_keys=True))
    return 0 if report.ready else 2


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
