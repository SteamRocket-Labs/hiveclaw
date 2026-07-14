from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.config import get_settings


def _manifest_paths(data_root: Path) -> list[Path]:
    return sorted(
        path
        for path in data_root.rglob("manifest.json")
        if path.is_file() and ".office_meta" in path.parts
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        "w",
        delete=False,
        dir=path.parent,
        encoding="utf-8",
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def retire_onlyoffice_metadata(data_root: Path, *, apply: bool) -> dict[str, int]:
    """Remove only the retired editor-session field from Office sidecars."""

    report = {"scanned": 0, "needs_update": 0, "updated": 0, "errors": 0}
    for manifest_path in _manifest_paths(data_root.resolve()):
        report["scanned"] += 1
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report["errors"] += 1
            continue
        if not isinstance(payload, dict):
            report["errors"] += 1
            continue
        if "active_editor_session" not in payload:
            continue
        report["needs_update"] += 1
        if not apply:
            continue
        payload.pop("active_editor_session", None)
        try:
            _atomic_write_json(manifest_path, payload)
        except OSError:
            report["errors"] += 1
            continue
        report["updated"] += 1
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Retire legacy Office editor-session metadata")
    parser.add_argument("--data-root", type=Path, default=None, help="Agent data root (defaults to AGENT_DATA_DIR)")
    parser.add_argument("--apply", action="store_true", help="Apply updates; default is dry-run")
    parser.add_argument("--confirm", action="store_true", help="Required together with --apply")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.apply and not args.confirm:
        raise SystemExit("--apply requires --confirm")
    data_root = args.data_root or Path(get_settings().AGENT_DATA_DIR)
    report = retire_onlyoffice_metadata(data_root, apply=args.apply)
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", **report}, sort_keys=True))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
