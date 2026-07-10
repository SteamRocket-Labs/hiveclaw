"""Audit RLS coverage across tenant/agent-scoped tables (Goal-2 isolation map).

P0-2: tenant isolation currently rests on app-level WHERE filters. Row-Level
Security exists but is **inert for most tables** because (a) the app connects as
the table owner — and an owner bypasses RLS unless the table is FORCEd — and
(b) several tenant/agent-scoped tables have no policy at all. This script
reports, against a live PostgreSQL, three buckets:

  - UNPROTECTED: a table carrying ``tenant_id``/``agent_id`` with RLS not even enabled
  - INERT:       RLS enabled but NOT forced while the app role owns the table
                 (policy present but bypassed in production)
  - ENFORCED:    genuinely enforced today (forced, or app role is not the owner)

It mutates nothing. Use it to drive the separate, staged migration that flips
the app to a non-owner role and moves every accessor onto
``tenant_scoped_session`` — the only safe way to make RLS a real backstop
without locking out the 37 bare-session call sites.

Usage:
    python -m app.scripts.audit_rls_coverage
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import text

from app.database import async_session


@dataclass(frozen=True)
class TableRls:
    """RLS state of one table, as read from the catalog."""

    table: str
    has_tenant_scope: bool  # carries tenant_id OR agent_id (tenant-derivable)
    enabled: bool
    forced: bool


@dataclass(frozen=True)
class RlsCoverageReport:
    unprotected: list[str]
    inert: list[str]
    enforced: list[str]


def analyze_rls_coverage(tables: list[TableRls], *, app_role_is_owner: bool) -> RlsCoverageReport:
    """Functional core: classify each tenant-scoped table's real enforcement.

    A table is genuinely enforced when it is FORCEd, or when the app role is not
    the table owner (then plain ENABLE already binds). Otherwise an enabled
    policy is inert (owner-bypass), and a table with no policy is unprotected.
    """
    unprotected: list[str] = []
    inert: list[str] = []
    enforced: list[str] = []
    for entry in tables:
        if not entry.has_tenant_scope:
            continue
        if not entry.enabled:
            unprotected.append(entry.table)
        elif app_role_is_owner and not entry.forced:
            inert.append(entry.table)
        else:
            enforced.append(entry.table)
    return RlsCoverageReport(sorted(unprotected), sorted(inert), sorted(enforced))


async def audit() -> RlsCoverageReport:
    """Imperative shell: read the catalog, classify, log the report."""
    async with async_session() as db:
        cols = await db.execute(
            text(
                "SELECT DISTINCT table_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name IN ('tenant_id', 'agent_id')"
            )
        )
        scoped = {row[0] for row in cols.all()}

        flags_rows = await db.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'r' AND n.nspname = 'public'"
            )
        )
        flags = {row[0]: (bool(row[1]), bool(row[2])) for row in flags_rows.all()}

        owner_row = await db.execute(
            text("SELECT current_user = tableowner FROM pg_tables WHERE tablename = 'agents' LIMIT 1")
        )
        app_role_is_owner = bool(owner_row.scalar_one_or_none())

        tables = [TableRls(name, name in scoped, enabled, forced) for name, (enabled, forced) in flags.items()]
        report = analyze_rls_coverage(tables, app_role_is_owner=app_role_is_owner)

        logger.info("[rls-audit] app role owns 'agents' table: {} (owner => ENABLE is inert)", app_role_is_owner)
        logger.warning("[rls-audit] UNPROTECTED ({}): {}", len(report.unprotected), report.unprotected)
        logger.warning("[rls-audit] INERT — enabled but owner-bypassed ({}): {}", len(report.inert), report.inert)
        logger.info("[rls-audit] ENFORCED ({}): {}", len(report.enforced), report.enforced)
        return report


def main() -> None:
    asyncio.run(audit())


if __name__ == "__main__":
    main()
