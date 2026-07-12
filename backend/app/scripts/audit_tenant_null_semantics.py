"""Read-only tenant-scope audit for the R-023 migration contract.

The command reports counts only; it never emits row payloads or changes data.
Run it before and after ``alembic upgrade head``::

    python -m app.scripts.audit_tenant_null_semantics
    python -m app.scripts.audit_tenant_null_semantics --fail-on-legacy-null
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import text

from app.database import Base, async_session, enter_rls_bypass
from app.db_bootstrap import (
    OPERATOR_NULLABLE_TENANT_TABLES,
    PLATFORM_SHARED_TENANT_PREDICATES,
    STRICT_TENANT_RLS_TABLES,
)
from app.models import import_all_models


AuthoritySource = tuple[str, str, str]

_MANUAL_AUTHORITY_SOURCES: dict[str, tuple[AuthoritySource, ...]] = {
    "decision_trace_feedback": (("decision_traces", "decision_id", "decision_id"),),
    "plaza_posts": (("agents", "author_id", "id"), ("users", "author_id", "id")),
    "runtime_budget_runs": (
        ("runtime_tasks", "root_runtime_task_id", "id"),
        ("agents", "root_agent_id", "id"),
        ("users", "root_user_id", "id"),
    ),
    "runtime_tasks": (
        ("agents", "parent_agent_id", "id"),
        ("agents", "child_agent_id", "id"),
        ("users", "root_user_id", "id"),
    ),
}


@dataclass(frozen=True)
class TenantScopeAuditRow:
    table: str
    database_nullable: bool
    null_rows: int
    uniquely_derivable: int
    conflicting_authority: int
    unresolved_authority: int
    quarantined_rows: int
    state: str


def authority_sources_for_table(table_name: str) -> tuple[AuthoritySource, ...]:
    """Return only tenant-authoritative parent joins for one table."""

    import_all_models()
    table = Base.metadata.tables[table_name]
    allowed_sources = set(STRICT_TENANT_RLS_TABLES) | set(OPERATOR_NULLABLE_TENANT_TABLES)
    discovered: set[AuthoritySource] = set(_MANUAL_AUTHORITY_SOURCES.get(table_name, ()))
    for foreign_key in table.foreign_keys:
        source_table = foreign_key.column.table.name
        if source_table not in allowed_sources:
            continue
        if source_table in PLATFORM_SHARED_TENANT_PREDICATES:
            continue
        if "tenant_id" not in foreign_key.column.table.c:
            continue
        discovered.add((source_table, foreign_key.parent.name, foreign_key.column.name))
    return tuple(sorted(discovered))


def classify_scope_state(*, null_rows: int, quarantined_rows: int) -> str:
    if null_rows:
        return "migration_required"
    if quarantined_rows:
        return "quarantined"
    return "strict"


def _candidate_union(table_name: str, sources: tuple[AuthoritySource, ...], quote) -> str | None:
    if not sources:
        return None
    parts = []
    for source_table, local_column, source_key in sources:
        parts.append(
            "SELECT source.tenant_id "
            f"FROM {quote(source_table)} AS source "
            f"WHERE source.{quote(source_key)} = target.{quote(local_column)} "
            "AND source.tenant_id IS NOT NULL"
        )
    return " UNION ALL ".join(parts)


async def audit_tenant_null_semantics(*, session_factory=None) -> list[TenantScopeAuditRow]:
    """Return a payload-free dry-run report against the live database."""

    import_all_models()
    factory = session_factory or async_session
    rows: list[TenantScopeAuditRow] = []
    async with factory() as db:
        async with enter_rls_bypass(db, reason="tenant NULL semantics read-only audit") as bypass_db:
            column_rows = (
                await bypass_db.execute(
                    text(
                        "SELECT table_name, is_nullable FROM information_schema.columns "
                        "WHERE table_schema='public' AND column_name='tenant_id'"
                    )
                )
            ).all()
            existing = {row.table_name: row.is_nullable == "YES" for row in column_rows}
            quarantine_exists = bool(
                await bypass_db.scalar(text("SELECT to_regclass('public.tenant_scope_quarantine_records')"))
            )
            quarantined: dict[str, int] = {}
            if quarantine_exists:
                quarantine_rows = (
                    await bypass_db.execute(
                        text(
                            "SELECT source_table, count(*) AS count "
                            "FROM tenant_scope_quarantine_records GROUP BY source_table"
                        )
                    )
                ).all()
                quarantined = {row.source_table: int(row.count) for row in quarantine_rows}

            quote = bypass_db.get_bind().dialect.identifier_preparer.quote
            for table_name in sorted(set(STRICT_TENANT_RLS_TABLES) & set(existing)):
                null_rows = int(
                    await bypass_db.scalar(text(f"SELECT count(*) FROM {quote(table_name)} WHERE tenant_id IS NULL"))
                    or 0
                )
                unique = conflict = unresolved = 0
                if null_rows:
                    sources = tuple(
                        source for source in authority_sources_for_table(table_name) if source[0] in existing
                    )
                    union = _candidate_union(table_name, sources, quote)
                    if union:
                        counts = (
                            await bypass_db.execute(
                                text(
                                    f"""
                                    SELECT
                                        count(*) FILTER (WHERE candidate_count = 1) AS unique_count,
                                        count(*) FILTER (WHERE candidate_count > 1) AS conflict_count,
                                        count(*) FILTER (WHERE candidate_count = 0) AS unresolved_count
                                    FROM (
                                        SELECT target.id, count(DISTINCT authority.tenant_id) AS candidate_count
                                        FROM {quote(table_name)} AS target
                                        LEFT JOIN LATERAL ({union}) AS authority ON true
                                        WHERE target.tenant_id IS NULL
                                        GROUP BY target.id
                                    ) AS candidates
                                    """
                                )
                            )
                        ).one()
                        unique = int(counts.unique_count or 0)
                        conflict = int(counts.conflict_count or 0)
                        unresolved = int(counts.unresolved_count or 0)
                    else:
                        unresolved = null_rows
                quarantined_rows = quarantined.get(table_name, 0)
                rows.append(
                    TenantScopeAuditRow(
                        table=table_name,
                        database_nullable=existing[table_name],
                        null_rows=null_rows,
                        uniquely_derivable=unique,
                        conflicting_authority=conflict,
                        unresolved_authority=unresolved,
                        quarantined_rows=quarantined_rows,
                        state=classify_scope_state(
                            null_rows=null_rows,
                            quarantined_rows=quarantined_rows,
                        ),
                    )
                )
    return rows


def _print_report(rows: list[TenantScopeAuditRow]) -> None:
    logger.info(
        "{:<42} {:>8} {:>8} {:>8} {:>8} {:>10}  {}",
        "table",
        "null",
        "derive",
        "conflict",
        "orphan",
        "quarantine",
        "state",
    )
    for row in rows:
        if row.null_rows or row.quarantined_rows or row.database_nullable:
            logger.info(
                "{:<42} {:>8} {:>8} {:>8} {:>8} {:>10}  {}",
                row.table,
                row.null_rows,
                row.uniquely_derivable,
                row.conflicting_authority,
                row.unresolved_authority,
                row.quarantined_rows,
                row.state,
            )
    logger.info(
        "[tenant-null-audit] tables={} legacy_null_rows={} conflicts={} unresolved={} quarantined={}",
        len(rows),
        sum(row.null_rows for row in rows),
        sum(row.conflicting_authority for row in rows),
        sum(row.unresolved_authority for row in rows),
        sum(row.quarantined_rows for row in rows),
    )


async def _amain(*, fail_on_legacy_null: bool) -> int:
    rows = await audit_tenant_null_semantics()
    _print_report(rows)
    return 2 if fail_on_legacy_null and any(row.null_rows for row in rows) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only tenant NULL semantics audit")
    parser.add_argument(
        "--fail-on-legacy-null",
        action="store_true",
        help="exit 2 when a tenant-owned NULL row still requires migration",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_amain(fail_on_legacy_null=args.fail_on_legacy_null)))


if __name__ == "__main__":
    main()
