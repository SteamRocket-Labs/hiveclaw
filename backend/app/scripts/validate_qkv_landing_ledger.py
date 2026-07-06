from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


_ATOMIC_ID_RE = re.compile(r"^[A-J]-\d{2}$")
_PLAN_MARKERS = (
    "P1 主架构计划",
    "P2 原子落地账本",
    "P3 Backfill / Rebuild / Compatibility 计划",
    "P4 验收 / 大召回 / 回滚计划",
)


def _markdown_cells(line: str) -> list[str]:
    if not line.startswith("|"):
        return []
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _atomic_rows(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        cells = _markdown_cells(line)
        if len(cells) < 5 or not _ATOMIC_ID_RE.match(cells[0]):
            continue
        rows.append(
            {
                "id": cells[0],
                "name": cells[1],
                "touchpoints": cells[2],
                "test_gate": cells[3],
                "evidence": cells[4],
            }
        )
    return rows


def validate_landing_ledger(doc_path: Path, *, expected_atomic_count: int = 48) -> dict[str, Any]:
    text = doc_path.read_text(encoding="utf-8")
    atomic_rows = _atomic_rows(text)
    ids = [row["id"] for row in atomic_rows]
    duplicate_ids = sorted({item for item in ids if ids.count(item) > 1})
    plan_count = sum(1 for marker in _PLAN_MARKERS if marker in text)
    evidence_cells = sum(1 for row in atomic_rows if row["evidence"].strip())

    errors: list[str] = []
    if "落地施工账本" not in text:
        errors.append("missing landing ledger status")
    if plan_count != len(_PLAN_MARKERS):
        errors.append("missing plan rows")
    if len(atomic_rows) != expected_atomic_count:
        errors.append("unexpected atomic count")
    if duplicate_ids:
        errors.append("duplicate atomic ids")
    if evidence_cells != len(atomic_rows):
        errors.append("missing evidence cells")
    if ids and ids != sorted(ids):
        errors.append("atomic ids are not sorted")

    return {
        "ok": not errors,
        "errors": errors,
        "doc_path": str(doc_path),
        "status_landing": "落地施工账本" in text,
        "plan_count": plan_count,
        "atomic_count": len(atomic_rows),
        "unique_atomic_count": len(set(ids)),
        "duplicate_ids": duplicate_ids,
        "first_id": ids[0] if ids else None,
        "last_id": ids[-1] if ids else None,
        "evidence_cells": evidence_cells,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the CCPlus QKV landing ledger document.")
    parser.add_argument("doc_path", type=Path)
    parser.add_argument("--expected-atomic-count", type=int, default=48)
    args = parser.parse_args()

    report = validate_landing_ledger(args.doc_path, expected_atomic_count=args.expected_atomic_count)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
