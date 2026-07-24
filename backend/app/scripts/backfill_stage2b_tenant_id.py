"""Retired Stage-2b compatibility surface.

R-023 replaced the partial updater (which deliberately left globally visible
NULL orphans) with the transactional ``tenant_null_semantics_0712`` migration.
This module remains import-compatible for operators and old automation, but is
read-only: use the canonical audit for dry-run evidence and Alembic for repair.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from loguru import logger

from app.scripts.audit_tenant_null_semantics import audit_tenant_null_semantics


class LegacyTenantBackfillRetiredError(RuntimeError):
    """Raised instead of running the unsafe partial Stage-2b mutation."""


@dataclass(frozen=True)
class BackfillSource:
    """Read-only compatibility descriptor for historical Stage-2b coverage."""

    table: str
    source_table: str
    local_fk: str
    source_pk: str


_STANDARD_TABLES = (
    "agent_activity_logs",
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
    "pending_reply_contexts",
    "tasks",
)

BACKFILL_PLAN: tuple[BackfillSource, ...] = (
    *(BackfillSource(table, "agents", "agent_id", "id") for table in _STANDARD_TABLES),
    BackfillSource("runtime_tasks", "agents", "parent_agent_id", "id"),
    BackfillSource("task_logs", "tasks", "task_id", "id"),
)


@dataclass(frozen=True)
class TableBackfillReport:
    table: str
    total_rows: int
    null_before: int
    will_fill: int
    orphan_residual: int
    updated: int | None = None


async def run_backfill(*, apply: bool, session_factory=None) -> list[TableBackfillReport]:
    """Return the current read-only audit in the legacy report shape."""

    if apply:
        raise LegacyTenantBackfillRetiredError(
            "Stage-2b partial writes are retired; run `alembic upgrade head` so "
            "R-023 can backfill, quarantine conflicts, add NOT NULL, and replace RLS atomically."
        )
    rows = await audit_tenant_null_semantics(session_factory=session_factory)
    return [
        TableBackfillReport(
            table=row.table,
            total_rows=row.null_rows,
            null_before=row.null_rows,
            will_fill=row.uniquely_derivable,
            orphan_residual=row.conflicting_authority + row.unresolved_authority,
        )
        for row in rows
    ]


def _print_report(reports: list[TableBackfillReport]) -> None:
    logger.warning("[stage2b-backfill] mutation retired; this is the canonical R-023 read-only compatibility report")
    for row in reports:
        if row.null_before:
            logger.info(
                "{} null={} derivable={} conflict_or_unresolved={}",
                row.table,
                row.null_before,
                row.will_fill,
                row.orphan_residual,
            )


async def _amain(*, apply: bool) -> int:
    try:
        reports = await run_backfill(apply=apply)
    except LegacyTenantBackfillRetiredError as exc:
        logger.error(str(exc))
        return 2
    _print_report(reports)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Retired Stage-2b tenant backfill compatibility command")
    parser.add_argument("--apply", action="store_true", help="rejected; use alembic upgrade head")
    parser.add_argument("--confirm", action="store_true", help="ignored compatibility flag")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_amain(apply=args.apply)))


if __name__ == "__main__":
    main()
