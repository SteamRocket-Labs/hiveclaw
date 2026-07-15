"""Dry-run or append exact legacy Knowledge provenance repair receipts.

Apply mode is intentionally double-gated. It appends auditable correction
events and never rewrites the original transcript or Knowledge body.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from app.database import async_session, enter_rls_bypass
from app.services.knowledge_provenance_repair import repair_legacy_knowledge_provenance


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=uuid.UUID, default=None)
    parser.add_argument("--agent-id", type=uuid.UUID, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    return parser


async def _main() -> None:
    args = _parser().parse_args()
    if args.apply and not args.confirm:
        raise SystemExit("--apply requires --confirm after reviewing the dry-run report")
    async with async_session() as db:
        async with enter_rls_bypass(
            db,
            reason="append-only cross-tenant Knowledge provenance repair",
        ) as bypass_db:
            report = await repair_legacy_knowledge_provenance(
                bypass_db,
                apply=args.apply,
                tenant_id=args.tenant_id,
                agent_id=args.agent_id,
                limit=args.limit,
            )
            if args.apply:
                await bypass_db.commit()
            else:
                await bypass_db.rollback()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
