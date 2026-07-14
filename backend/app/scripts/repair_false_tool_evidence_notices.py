"""Dry-run or apply the exact retired final-answer verifier repair.

Apply mode changes product read models and appends audited transcript events;
it therefore requires both ``--apply`` and ``--confirm``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from app.database import async_session, enter_rls_bypass
from app.services.false_tool_evidence_repair import repair_false_tool_evidence_notices


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=uuid.UUID, default=None)
    parser.add_argument("--agent-id", type=uuid.UUID, default=None)
    parser.add_argument("--recent-days", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    return parser


async def _main() -> None:
    args = _parser().parse_args()
    if args.apply and not args.confirm:
        raise SystemExit("--apply is irreversible for the product read model; pass --confirm after reviewing dry-run")
    async with async_session() as db:
        async with enter_rls_bypass(
            db,
            reason="audited cross-tenant repair of exact retired tool-evidence verifier notices",
        ) as bypass_db:
            report = await repair_false_tool_evidence_notices(
                bypass_db,
                apply=args.apply,
                tenant_id=args.tenant_id,
                agent_id=args.agent_id,
                recent_days=args.recent_days,
                limit=args.limit,
            )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
