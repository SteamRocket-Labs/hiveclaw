"""Settle trigger RuntimeTasks that were stranded by fire-and-forget dispatch.

Before the P0-B fix the trigger daemon persisted its ledger row as ``running``
and then spawned the agent invocation with a discarded ``asyncio.create_task``.
A run that died before writing anything left a row with no lease, no terminal
state and no observer: production accumulated 2,107 such rows between
2026-07-16 and 2026-08-23, every one of them with zero invocation spans and zero
``trigger_run`` chat sessions.

The fix routes new fires through the RuntimeTask worker, so no further orphans
can be created. This script settles the ones already on disk.

Each row is classified from mechanical facts only:

``needs_reconciliation``
    the run bound a child session, so tools may have executed and external side
    effects may exist. A human decides whether it was completed.

``skipped`` (``orphaned_trigger_dispatch``)
    the run never bound a session and produced no spans, so nothing external
    happened. The fire is simply lost; the next tick re-fires whatever is due.

Dry-run by default. Applying is irreversible, so it needs both ``--apply`` and
the exact confirmation phrase.

    python -m app.scripts.reconcile_orphaned_trigger_runs
    python -m app.scripts.reconcile_orphaned_trigger_runs --apply --confirm RECONCILE_ORPHANED_TRIGGER_RUNS
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.runtime_task import RuntimeTask


CONFIRM_PHRASE = "RECONCILE_ORPHANED_TRIGGER_RUNS"
_SESSION_BOUND_SUMMARY = (
    "Trigger run was stranded by fire-and-forget dispatch after binding a session; "
    "replay could duplicate side effects, so it requires reconciliation."
)
_LOST_SUMMARY = (
    "Trigger run was stranded by fire-and-forget dispatch before binding a session or emitting any span; "
    "no external effect occurred and the fire is abandoned."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_session_bound(task: RuntimeTask) -> bool:
    metadata = dict(getattr(task, "metadata_json", None) or {})
    return bool(getattr(task, "child_session_id", None) or metadata.get("session_id") or metadata.get("session_bound"))


async def _collect_orphans(*, older_than_minutes: int) -> list[tuple[Any, Any]]:
    """Locator-only cross-tenant scan; mutations reopen under tenant RLS."""
    cutoff = _utcnow() - timedelta(minutes=max(0, older_than_minutes))
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="reconcile orphaned trigger runtime tasks"),
    ):
        rows = await db.execute(
            select(RuntimeTask.id, RuntimeTask.tenant_id).where(
                RuntimeTask.task_type == "trigger",
                RuntimeTask.status == "running",
                RuntimeTask.created_at < cutoff,
            )
        )
        return list(rows.all())


async def reconcile_orphaned_trigger_runs(*, apply: bool, older_than_minutes: int) -> dict[str, Any]:
    locators = await _collect_orphans(older_than_minutes=older_than_minutes)
    by_tenant: dict[Any, list[Any]] = {}
    for task_id, tenant_id in locators:
        if tenant_id is None:
            continue
        by_tenant.setdefault(tenant_id, []).append(task_id)

    report: dict[str, Any] = {
        "scanned": len(locators),
        "tenantless_skipped": sum(1 for _task_id, tenant_id in locators if tenant_id is None),
        "needs_reconciliation": 0,
        "abandoned": 0,
        "applied": bool(apply),
        "oldest_created_at": None,
        "newest_created_at": None,
        "samples": [],
    }

    for tenant_id, task_ids in by_tenant.items():
        async with tenant_scoped_session(
            tenant_id,
            require_tenant=True,
            source="reconcile_orphaned_trigger_runs",
        ) as db:
            tasks = list(
                (
                    await db.execute(
                        select(RuntimeTask).where(
                            RuntimeTask.id.in_(task_ids),
                            RuntimeTask.task_type == "trigger",
                            RuntimeTask.status == "running",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for task in tasks:
                created_at = getattr(task, "created_at", None)
                if created_at is not None:
                    stamp = created_at.isoformat()
                    if report["oldest_created_at"] is None or stamp < report["oldest_created_at"]:
                        report["oldest_created_at"] = stamp
                    if report["newest_created_at"] is None or stamp > report["newest_created_at"]:
                        report["newest_created_at"] = stamp

                session_bound = _is_session_bound(task)
                if session_bound:
                    report["needs_reconciliation"] += 1
                    target_status = "needs_reconciliation"
                    summary = _SESSION_BOUND_SUMMARY
                    blocker = "session_bound_mutating_trigger"
                else:
                    report["abandoned"] += 1
                    target_status = "skipped"
                    summary = _LOST_SUMMARY
                    blocker = "orphaned_trigger_dispatch"

                if len(report["samples"]) < 10:
                    report["samples"].append(
                        {
                            "runtime_task_id": task.id.hex,
                            "created_at": created_at.isoformat() if created_at else None,
                            "target_status": target_status,
                            "blocker": blocker,
                        }
                    )

                if not apply:
                    continue

                metadata = dict(getattr(task, "metadata_json", None) or {})
                metadata["orphan_reconciliation"] = {
                    "blocker": blocker,
                    "reconciled_at": _utcnow().isoformat(),
                    "previous_status": "running",
                    "reason": "fire_and_forget_dispatch",
                }
                if not session_bound:
                    metadata["skip_reason"] = "orphaned_trigger_dispatch"
                task.status = target_status
                task.result_summary = task.result_summary or summary
                task.completed_at = _utcnow()
                task.metadata_json = metadata
            if apply and tasks:
                await db.commit()

    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the settled statuses (default: dry run)")
    parser.add_argument("--confirm", default="", help=f"must be {CONFIRM_PHRASE} when --apply is used")
    parser.add_argument(
        "--older-than-minutes",
        type=int,
        default=60,
        help="only settle rows older than this, so live runs are never touched (default: 60)",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.apply and args.confirm != CONFIRM_PHRASE:
        print(f"--apply requires --confirm {CONFIRM_PHRASE}")
        return 2
    report = asyncio.run(reconcile_orphaned_trigger_runs(apply=args.apply, older_than_minutes=args.older_than_minutes))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
