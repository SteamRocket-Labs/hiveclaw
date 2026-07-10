"""Backfill Agent-workspace Skill/Subagent assets into the thin control index."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session, tenant_scoped_session
from app.models.tenant import Tenant
from app.services.ai_assets import backfill_file_native_assets


async def run(*, apply: bool, data_root: Path | None = None) -> list[dict]:
    async with async_session() as db:
        tenant_ids = list((await db.execute(select(Tenant.id).order_by(Tenant.id))).scalars())
    reports: list[dict] = []
    root = Path(data_root or get_settings().AGENT_DATA_DIR)
    for tenant_id in tenant_ids:
        async with tenant_scoped_session(tenant_id, require_tenant=True, source="ai_asset_backfill") as db:
            reports.append(
                await backfill_file_native_assets(
                    db,
                    tenant_id=tenant_id,
                    data_root=root,
                    apply=apply,
                )
            )
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write missing/drifted control records")
    parser.add_argument("--confirm", action="store_true", help="Required together with --apply")
    args = parser.parse_args()
    if args.apply and not args.confirm:
        parser.error("--apply requires --confirm")
    reports = asyncio.run(run(apply=args.apply))
    print(json.dumps(reports, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
