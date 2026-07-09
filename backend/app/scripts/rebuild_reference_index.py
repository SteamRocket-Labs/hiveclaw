"""Rebuild derived memory reference indexes.

Dry-run by default. The SQLite index is a rebuildable accelerator, so this
script never mutates T0/T2/T3 truth files; ``--apply`` only recreates
``memory/indexes/index.sqlite`` from Markdown/JSONL sources.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.memory.reference_index import rebuild_reference_index


def _iter_agent_ids(data_root: Path) -> list[str]:
    if not data_root.exists():
        return []
    agent_ids: list[str] = []
    for child in sorted(data_root.iterdir()):
        if child.is_dir() and (child / "memory").exists():
            agent_ids.append(child.name)
    return agent_ids


def rebuild_reference_indexes(
    *,
    data_root: Path | str,
    agent_id: object | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    root = Path(data_root)
    agent_ids = [str(agent_id)] if agent_id is not None else _iter_agent_ids(root)
    reports: list[dict[str, Any]] = []
    for current_agent_id in agent_ids:
        if not apply:
            reports.append({"agent_id": current_agent_id, "status": "dry_run"})
            continue
        rebuilt = rebuild_reference_index(agent_id=current_agent_id, data_root=root)
        reports.append(
            {
                "agent_id": rebuilt.agent_id,
                "status": "rebuilt",
                "referrers": rebuilt.referrers,
                "refs": rebuilt.refs,
                "packages": rebuilt.packages,
                "label_axis_rows": rebuilt.label_axis_rows,
                "debt_history_rows": rebuilt.debt_history_rows,
            }
        )
    return {
        "apply": bool(apply),
        "data_root": str(root),
        "agent_count": len(agent_ids),
        "agents": reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild Hive memory reference indexes from truth files.")
    parser.add_argument("--data-root", default=None, help="Agent data root; defaults to AGENT_DATA_DIR.")
    parser.add_argument("--agent-id", default=None, help="Optional single agent id.")
    parser.add_argument("--apply", action="store_true", help="Rebuild indexes. Default is dry-run.")
    args = parser.parse_args()

    data_root = Path(args.data_root or get_settings().AGENT_DATA_DIR)
    report = rebuild_reference_indexes(data_root=data_root, agent_id=args.agent_id, apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
