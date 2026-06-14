"""Backfill existing ``Tool(type="mcp")`` rows to the canonical
``mcp__server__tool`` name (Step 6 of docs/cc-tooling-alignment-and-plugin-system.md).

**Dry-run by default** — prints the planned renames, mutates nothing.
``--apply --confirm`` executes the ``UPDATE tools SET name=...``. This is an
irreversible production data step, so it sits behind an owner confirmation gate
(the one exception to the one-pass delivery discipline — a safety gate, not an
MVP stage). The printed JSON report (``tool_id``/``old_name``/``new_name`` per
tenant) IS the reversal record: swap old/new to roll back.

Safe to deploy canonical generation BEFORE running this — ``_execute_mcp_tool``
resolves a canonical name against a still-legacy row via the canonical alias, so
execution never depends on the backfill having run. Re-running is idempotent
(already-canonical rows are skipped).

Runs under ``enter_rls_bypass`` so it works both today (owner connection) and
after the stage-3 RLS role flip. Cross-tenant by design and audited.

Usage::

    python -m app.scripts.backfill_mcp_tool_names              # dry-run report
    python -m app.scripts.backfill_mcp_tool_names --apply --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import pkgutil

from sqlalchemy import select, update

from app.database import async_session, enter_rls_bypass
from app.services.mcp_naming import McpNameRow, plan_mcp_name_canonicalization


def _load_all_models() -> None:
    import app.models as _models_pkg

    for _, name, _ in pkgutil.iter_modules(_models_pkg.__path__):
        importlib.import_module(f"app.models.{name}")


async def _run(apply: bool) -> dict:
    _load_all_models()
    from app.models.tool import Tool

    async with async_session() as db:
        async with enter_rls_bypass(db, reason="Step 6 MCP canonical name backfill") as bdb:
            rows = (await bdb.execute(select(Tool).where(Tool.type == "mcp"))).scalars().all()
            name_rows = [
                McpNameRow(
                    tool_id=str(t.id),
                    name=t.name,
                    mcp_server_name=t.mcp_server_name,
                    mcp_tool_name=t.mcp_tool_name,
                    tenant_id=str(t.tenant_id) if t.tenant_id else None,
                )
                for t in rows
            ]
            plan = plan_mcp_name_canonicalization(name_rows)

            if apply and plan:
                for rename in plan:
                    await bdb.execute(update(Tool).where(Tool.id == rename.tool_id).values(name=rename.new_name))
                await bdb.commit()

    return {
        "mcp_tool_rows": len(name_rows),
        "renames_planned": len(plan),
        "applied": bool(apply and plan),
        "renames": [
            {
                "tool_id": r.tool_id,
                "tenant_id": r.tenant_id,
                "old_name": r.old_name,
                "new_name": r.new_name,
            }
            for r in plan
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill MCP Tool.name to canonical mcp__server__tool form.")
    parser.add_argument("--apply", action="store_true", help="Apply renames. Default is dry-run.")
    parser.add_argument("--confirm", action="store_true", help="Required with --apply.")
    args = parser.parse_args()

    if args.apply and not args.confirm:
        raise SystemExit("--apply requires --confirm")

    report = asyncio.run(_run(apply=args.apply))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
