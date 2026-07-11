"""Backfill shared Workspace ownership evidence; dry-run by default."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session, tenant_scoped_session
from app.models.tenant import Tenant
from app.services.workspace_resource_authority import backfill_legacy_workspace_resources


async def run(*, apply: bool, data_root: Path | None = None) -> list[dict]:
    async with async_session() as db:
        tenant_ids = list((await db.execute(select(Tenant.id).order_by(Tenant.id))).scalars())
    root = Path(data_root or get_settings().AGENT_DATA_DIR)
    reports: list[dict] = []
    for tenant_id in tenant_ids:
        async with tenant_scoped_session(
            tenant_id,
            require_tenant=True,
            source="workspace_resource_backfill",
        ) as db:
            reports.append(
                await backfill_legacy_workspace_resources(
                    db,
                    tenant_id=tenant_id,
                    data_root=root,
                    apply=apply,
                )
            )
            if apply:
                await db.commit()
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default=None, help="Agent data root; defaults to AGENT_DATA_DIR")
    parser.add_argument("--apply", action="store_true", help="Persist quarantine/hash/tombstone evidence")
    parser.add_argument("--confirm", action="store_true", help="Required together with --apply")
    args = parser.parse_args()
    if args.apply and not args.confirm:
        parser.error("--apply requires --confirm")
    reports = asyncio.run(
        run(
            apply=args.apply,
            data_root=Path(args.data_root) if args.data_root else None,
        )
    )
    print(json.dumps(reports, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
