"""Repair legacy peer digital-employee A2A Session projections.

Dry-run by default. The apply form is intentionally double-gated:

    python -m app.scripts.repair_peer_delegation_sessions
    python -m app.scripts.repair_peer_delegation_sessions --apply --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.services.delegation_session_repair import repair_peer_delegation_session_projections


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair legacy peer-A2A child and parent Session projections.")
    parser.add_argument("--apply", action="store_true", help="Apply the repair. Default is dry-run.")
    parser.add_argument("--confirm", action="store_true", help="Required with --apply.")
    parser.add_argument("--parent-session-id", default=None, help="Optionally limit repair to one parent Session UUID.")
    parser.add_argument("--limit", type=int, default=5000, help="Maximum terminal delegation tasks to scan.")
    args = parser.parse_args()
    if args.apply and not args.confirm:
        raise SystemExit("--apply requires --confirm")
    report = asyncio.run(
        repair_peer_delegation_session_projections(
            apply=args.apply,
            parent_session_id=args.parent_session_id,
            limit=args.limit,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
