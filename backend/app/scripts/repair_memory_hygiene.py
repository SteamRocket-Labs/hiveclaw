"""Repair D1/D2/D8/D10 memory hygiene across all agent workspaces.

Dry-run by default. ``--apply --confirm`` mutates the agent data directory by
rewriting dirty T3 prose and quarantining retired artifacts under
``memory/retired_artifacts``. All moved artifacts and original dirty lines are
recorded in ``memory/archive.md``.

Usage:
    python -m app.scripts.repair_memory_hygiene
    python -m app.scripts.repair_memory_hygiene --apply --confirm
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import get_settings
from app.memory.hygiene import repair_all_memory_hygiene


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair Hive agent memory hygiene.")
    parser.add_argument("--data-root", default=None, help="Agent data root; defaults to AGENT_DATA_DIR.")
    parser.add_argument("--apply", action="store_true", help="Apply changes. Default is dry-run.")
    parser.add_argument("--confirm", action="store_true", help="Required with --apply.")
    args = parser.parse_args()

    if args.apply and not args.confirm:
        raise SystemExit("--apply requires --confirm")

    data_root = Path(args.data_root or get_settings().AGENT_DATA_DIR)
    report = repair_all_memory_hygiene(data_root, dry_run=not args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
