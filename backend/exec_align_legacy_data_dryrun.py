"""Dry-run inventory (+ guarded cleanup) for exec/automation CC-alignment legacy data.

The exec/automation CC-alignment (docs/trigger-cc-alignment.md) retired the
objective subsystem (#4), the supervision task type (#6), and the focus.md
projection. Schema is handled by Alembic on deploy (the objective table + the 4
supervision columns are dropped). This script covers the DATA-level residue on an
already-running deployment — it is the "dry-run + confirmation" gate the Delivery
Discipline requires for production data (the one exception to one-pass delivery).

Residue and why it is already SAFE (inert), not urgent:
  * Triggers with ``config.trigger_class == 'objective_task'`` — the daemon no
    longer special-cases this class; without a ``config.plan_id`` they now
    fail-closed at the plan-gate backstop (blocked, never run). Cleanup is hygiene.
  * Tasks with ``type == 'supervision'`` — execute_task only handles ``todo`` now,
    so these never execute. Cleanup is hygiene.
  * ``focus.md`` files under AGENT_DATA_DIR — no longer projected into any prompt;
    they are ordinary workspace scratch files now (left in place by default).

Usage (run on the deployment, e.g. Railway):
    python exec_align_legacy_data_dryrun.py            # read-only inventory
    python exec_align_legacy_data_dryrun.py --apply    # disable/delete + backup (asks first)

``--apply`` is OFF by default. It writes a JSON backup next to this script BEFORE
any mutation, then DISABLES objective_task triggers (reversible: is_enabled=False)
and DELETES supervision task rows (backed up). It never touches focus.md files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from app.database import async_session

_OBJECTIVE_TRIGGER_SQL = """
SELECT id, agent_id, name, is_enabled, config->>'objective_id' AS objective_id
FROM agent_triggers
WHERE config->>'trigger_class' = 'objective_task'
ORDER BY agent_id
"""

_SUPERVISION_TASK_SQL = """
SELECT id, agent_id, title, status
FROM tasks
WHERE type = 'supervision'
ORDER BY agent_id
"""


async def _fetch(conn, sql: str) -> list[dict]:
    rows = (await conn.execute(text(sql))).mappings().all()
    return [dict(r) for r in rows]


async def _inventory(conn) -> dict:
    objective_triggers = await _fetch(conn, _OBJECTIVE_TRIGGER_SQL)
    supervision_tasks = await _fetch(conn, _SUPERVISION_TASK_SQL)
    # AgentObjective table is dropped by retire_agent_objectives_0608; report if it
    # somehow still exists (migration not yet applied on this deployment).
    table_check = (
        await conn.execute(text("SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_objectives'"))
    ).scalar()
    return {
        "objective_task_triggers": objective_triggers,
        "supervision_tasks": supervision_tasks,
        "agent_objectives_table_still_present": bool(table_check),
    }


def _print_inventory(inv: dict) -> None:
    triggers = inv["objective_task_triggers"]
    tasks = inv["supervision_tasks"]
    print("=== exec/automation legacy-data inventory (read-only) ===")
    print(f"objective_task triggers (inert, fail-closed): {len(triggers)}")
    for t in triggers[:20]:
        print(f"  - trigger {t['id']} agent={t['agent_id']} name={t['name']!r} enabled={t['is_enabled']}")
    if len(triggers) > 20:
        print(f"  ... and {len(triggers) - 20} more")
    print(f"supervision tasks (inert, never execute): {len(tasks)}")
    for t in tasks[:20]:
        print(f"  - task {t['id']} agent={t['agent_id']} title={t['title']!r} status={t['status']}")
    if len(tasks) > 20:
        print(f"  ... and {len(tasks) - 20} more")
    if inv["agent_objectives_table_still_present"]:
        print("WARNING: agent_objectives table still present — apply migrations (retire_agent_objectives_0608) first.")
    print("=========================================================")


def _write_backup(inv: dict) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = f"exec_align_legacy_backup_{stamp}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(inv, fh, indent=2, default=str)
    return path


async def _apply(conn) -> None:
    # Disable (reversible) the objective_task triggers; DELETE supervision tasks.
    disabled = (
        await conn.execute(
            text(
                "UPDATE agent_triggers SET is_enabled = false "
                "WHERE config->>'trigger_class' = 'objective_task' AND is_enabled = true"
            )
        )
    ).rowcount
    deleted = (await conn.execute(text("DELETE FROM tasks WHERE type = 'supervision'"))).rowcount
    await conn.commit()
    print(f"applied: disabled {disabled} objective_task trigger(s), deleted {deleted} supervision task(s)")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="disable/delete the legacy data (default: dry-run only)")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation for --apply")
    args = parser.parse_args()

    async with async_session() as conn:
        inv = await _inventory(conn)
        _print_inventory(inv)

        if not args.apply:
            print("\nDry-run only. Re-run with --apply to disable/delete (a JSON backup is written first).")
            return 0

        backup_path = _write_backup(inv)
        print(f"\nbackup written: {backup_path}")
        if not args.yes:
            reply = input("Proceed to DISABLE objective_task triggers + DELETE supervision tasks? [y/N] ").strip()
            if reply.lower() not in {"y", "yes"}:
                print("aborted (no changes made).")
                return 1
        await _apply(conn)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
