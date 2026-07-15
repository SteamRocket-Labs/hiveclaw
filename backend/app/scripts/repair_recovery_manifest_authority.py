"""Dry-run/apply fleet repair for pre-authority RecoveryManifest files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import get_settings
from app.runtime.recovery_manifest_legacy_repair import repair_legacy_recovery_manifests


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.apply and not args.confirm:
        parser.error("--apply requires --confirm")
    if args.confirm and not args.apply:
        parser.error("--confirm is valid only with --apply")
    data_root = args.data_root or Path(get_settings().AGENT_DATA_DIR)
    report = repair_legacy_recovery_manifests(data_root, apply=args.apply)
    print(json.dumps(report.to_payload(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
