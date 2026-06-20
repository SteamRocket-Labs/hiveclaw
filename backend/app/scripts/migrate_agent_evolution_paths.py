"""Migrate retired agent evolution paths into the unified memory layout.

This script is intentionally non-destructive: ``--apply`` copies legacy files
into memory archive/staging paths and leaves the original files in place for
audit/recovery. Runtime code no longer reads the retired paths.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings

LEGACY_EVOLUTION_FILES = ("scorecard.md", "lineage.md", "blocklist.md")


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _rel(workspace: Path, path: Path) -> str:
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return path.as_posix()


def migrate_workspace(workspace: Path, *, apply: bool = False) -> dict[str, Any]:
    workspace = workspace.resolve()
    planned: list[str] = []
    applied: list[str] = []

    legacy_root = workspace / "memory" / ".legacy" / "evolution"
    for filename in LEGACY_EVOLUTION_FILES:
        src = workspace / "evolution" / filename
        if not src.exists():
            continue
        dst = legacy_root / filename
        operation = f"copy_legacy_file:{_rel(workspace, src)}"
        planned.append(operation)
        if apply:
            _copy_file(src, dst)
            applied.append(operation)

    legacy_soul_candidates = workspace / "evolution" / "soul_candidates"
    if legacy_soul_candidates.exists():
        for candidate_dir in sorted(path for path in legacy_soul_candidates.iterdir() if path.is_dir()):
            dst = workspace / "memory" / ".staging" / "soul_candidates" / candidate_dir.name
            operation = f"copy_soul_candidate:{_rel(workspace, candidate_dir)}"
            planned.append(operation)
            if apply:
                _copy_tree(candidate_dir, dst)
                applied.append(operation)

    legacy_soul_rollback = workspace / "evolution" / "rollback" / "soul"
    if legacy_soul_rollback.exists():
        for rollback_file in sorted(path for path in legacy_soul_rollback.iterdir() if path.is_file()):
            dst = workspace / "memory" / ".rollback" / "soul" / rollback_file.name
            operation = f"copy_soul_rollback:{_rel(workspace, rollback_file)}"
            planned.append(operation)
            if apply:
                _copy_file(rollback_file, dst)
                applied.append(operation)

    report: dict[str, Any] = {
        "schema": "agent_evolution_path_migration.v1",
        "workspace": str(workspace),
        "apply": apply,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "legacy_sources_retained": True,
        "planned_operations": planned,
        "applied_operations": applied,
    }
    if apply:
        report_path = legacy_root / "migration_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _iter_workspaces(data_root: Path) -> list[Path]:
    if not data_root.exists():
        return []
    return sorted(path for path in data_root.iterdir() if path.is_dir())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, help="Single agent workspace to inspect or migrate.")
    parser.add_argument("--data-root", type=Path, help="Agent data root; defaults to AGENT_DATA_DIR.")
    parser.add_argument("--apply", action="store_true", help="Copy legacy artifacts into unified memory paths.")
    args = parser.parse_args()

    if args.workspace:
        workspaces = [args.workspace]
    else:
        data_root = args.data_root or Path(get_settings().AGENT_DATA_DIR)
        workspaces = _iter_workspaces(data_root)

    reports = [migrate_workspace(workspace, apply=args.apply) for workspace in workspaces]
    print(json.dumps({"workspaces": reports}, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
