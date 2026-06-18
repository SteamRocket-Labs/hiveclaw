"""Stage-2b backfill: populate ``tenant_id`` on the agent-scoped tables that
``rls_stage2b_agent_scoped_0610`` just gave a (nullable) ``tenant_id`` column.

**Dry-run by default** — counts only, mutates nothing. ``--apply --confirm``
executes the UPDATEs. This is an irreversible production data step, so it sits
behind an owner confirmation gate (the one exception to the one-pass delivery
discipline — a safety gate, not an MVP stage).

Source of tenant_id (the column is filled from the row's owning agent / parent):

* standard agent-owned tables — ``agent_id`` → ``agents.tenant_id``
* ``runtime_tasks`` — ``parent_agent_id`` → ``agents.tenant_id`` (nullable source:
  delegation rows with no parent stay NULL)
* ``task_logs`` — ``task_id`` → ``tasks.tenant_id`` (MUST run after ``tasks``;
  ordered last in the plan)

Every UPDATE carries ``source.tenant_id IS NOT NULL`` so the backfill never
writes a NULL — rows whose source is missing or itself tenant-less are reported
as ``orphan`` residue (they keep tenant_id NULL, which the policy's ``OR
tenant_id IS NULL`` clause leaves globally visible — surface them, don't hide).

SQL is built with SQLAlchemy Core against the live metadata (no string-formatted
identifiers). Runs under ``enter_rls_bypass`` so it works both today (owner
connection) and after the stage-3 role flip (non-owner). Cross-tenant by design
and audited.

Usage::

    python -m app.scripts.backfill_stage2b_tenant_id              # dry-run report
    python -m app.scripts.backfill_stage2b_tenant_id --apply --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import pkgutil
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Base, async_session, enter_rls_bypass


def _load_all_models() -> None:
    """Import every app.models submodule so Base.metadata is fully populated
    (a bare script import does not trigger SQLAlchemy table registration)."""
    import app.models as _models_pkg

    for _, name, _ in pkgutil.iter_modules(_models_pkg.__path__):
        importlib.import_module(f"app.models.{name}")


_load_all_models()


@dataclass(frozen=True)
class BackfillSource:
    """How one table derives its tenant_id (functional-core descriptor)."""

    table: str
    source_table: str  # agents | tasks
    local_fk: str  # column on `table` pointing at source_table's pk
    source_pk: str  # pk column on source_table (always "id" here)


# Ordered plan. Standard agent_id tables first (alphabetical — includes `tasks`),
# then runtime_tasks (parent_agent_id), then task_logs LAST (depends on tasks
# already being backfilled). Mirrors rls_stage2b_agent_scoped_0610._STAGE2B_TABLES.
_STANDARD_TABLES = (
    "agent_activity_logs",
    "agent_agent_relationships",
    "agent_capability_installs",
    "agent_permissions",
    "agent_relationships",
    "agent_plan_requests",
    "agent_schedules",
    "agent_tools",
    "agent_triggers",
    "approval_requests",
    "audit_logs",
    "channel_configs",
    "chat_messages",
    "chat_sessions",
    "gateway_messages",
    "pending_reply_contexts",
    "tasks",
)

BACKFILL_PLAN: tuple[BackfillSource, ...] = (
    *(BackfillSource(t, "agents", "agent_id", "id") for t in _STANDARD_TABLES),
    BackfillSource("runtime_tasks", "agents", "parent_agent_id", "id"),
    BackfillSource("task_logs", "tasks", "task_id", "id"),  # AFTER tasks
)


@dataclass(frozen=True)
class TableBackfillReport:
    table: str
    total_rows: int
    null_before: int
    will_fill: int  # rows whose source exists AND source.tenant_id IS NOT NULL
    orphan_residual: int  # null rows the backfill cannot fill (source missing/tenant-less)
    updated: int | None = None  # populated only in --apply mode


def _count_total(src: BackfillSource):
    t = Base.metadata.tables[src.table]
    return select(func.count()).select_from(t)


def _count_null(src: BackfillSource):
    t = Base.metadata.tables[src.table]
    return select(func.count()).select_from(t).where(t.c.tenant_id.is_(None))


def _count_will_fill(src: BackfillSource):
    """Null rows whose source row exists and itself carries a tenant_id."""
    t = Base.metadata.tables[src.table]
    s = Base.metadata.tables[src.source_table]
    return (
        select(func.count())
        .select_from(t.join(s, t.c[src.local_fk] == s.c[src.source_pk]))
        .where(t.c.tenant_id.is_(None), s.c.tenant_id.isnot(None))
    )


def _update_stmt(src: BackfillSource):
    """UPDATE t SET tenant_id = s.tenant_id FROM s WHERE … — never writes NULL."""
    t = Base.metadata.tables[src.table]
    s = Base.metadata.tables[src.source_table]
    return (
        update(t)
        .where(
            t.c[src.local_fk] == s.c[src.source_pk],
            t.c.tenant_id.is_(None),
            s.c.tenant_id.isnot(None),
        )
        .values(tenant_id=s.c.tenant_id)
    )


async def _scalar(db: AsyncSession, stmt) -> int:
    return (await db.execute(stmt)).scalar_one()


async def run_backfill(*, apply: bool, session_factory=None) -> list[TableBackfillReport]:
    """Imperative shell: measure every table; optionally apply the UPDATEs.

    In --apply mode the plan order matters: tasks is backfilled (inside the
    standard batch) before task_logs derives from it, all in one bypass scope.

    ``session_factory`` lets integration tests drive a Testcontainers engine;
    production passes None and uses the global ``async_session``.
    """
    factory = session_factory or async_session
    reports: list[TableBackfillReport] = []
    async with factory() as db:
        async with enter_rls_bypass(db, reason="stage-2b tenant_id backfill") as bdb:
            for src in BACKFILL_PLAN:
                total = await _scalar(bdb, _count_total(src))
                null_before = await _scalar(bdb, _count_null(src))
                will_fill = await _scalar(bdb, _count_will_fill(src))
                if apply:
                    await bdb.execute(_update_stmt(src))
                    null_after = await _scalar(bdb, _count_null(src))
                    reports.append(
                        TableBackfillReport(
                            table=src.table,
                            total_rows=total,
                            null_before=null_before,
                            will_fill=will_fill,
                            orphan_residual=null_after,
                            updated=null_before - null_after,
                        )
                    )
                else:
                    reports.append(
                        TableBackfillReport(
                            table=src.table,
                            total_rows=total,
                            null_before=null_before,
                            will_fill=will_fill,
                            orphan_residual=null_before - will_fill,
                        )
                    )
            if apply:
                await bdb.commit()
    return reports


def _print_report(reports: list[TableBackfillReport], *, apply: bool) -> None:
    mode = "APPLIED" if apply else "DRY-RUN (no changes written)"
    logger.info("[stage2b-backfill] {} — {} tables", mode, len(reports))
    header = f"{'table':<28} {'total':>8} {'null_before':>12} {'will_fill':>10}"
    header += f" {'updated':>8}" if apply else ""
    header += f" {'orphan_null':>12}"
    logger.info(header)
    total_fill = total_orphan = 0
    for r in reports:
        line = f"{r.table:<28} {r.total_rows:>8} {r.null_before:>12} {r.will_fill:>10}"
        if apply:
            line += f" {r.updated if r.updated is not None else 0:>8}"
        line += f" {r.orphan_residual:>12}"
        logger.info(line)
        total_fill += r.will_fill
        total_orphan += r.orphan_residual
    logger.info("[stage2b-backfill] total will_fill={} orphan_null_residual={}", total_fill, total_orphan)
    if total_orphan:
        logger.warning(
            "[stage2b-backfill] {} rows keep tenant_id NULL (source agent/task missing or itself "
            "tenant-less). The policy's `OR tenant_id IS NULL` leaves these globally visible — "
            "review whether they should be deleted or assigned before the role flip.",
            total_orphan,
        )


async def _amain(apply: bool, confirm: bool) -> None:
    if apply and not confirm:
        logger.warning(
            "[stage2b-backfill] --apply requires --confirm (irreversible data write). "
            "Showing dry-run instead; re-run with `--apply --confirm` to execute."
        )
        apply = False
    reports = await run_backfill(apply=apply)
    _print_report(reports, apply=apply)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage-2b tenant_id backfill (dry-run by default).")
    parser.add_argument("--apply", action="store_true", help="execute the UPDATEs (needs --confirm)")
    parser.add_argument("--confirm", action="store_true", help="confirmation gate for --apply")
    args = parser.parse_args()
    asyncio.run(_amain(apply=args.apply, confirm=args.confirm))


if __name__ == "__main__":
    main()
