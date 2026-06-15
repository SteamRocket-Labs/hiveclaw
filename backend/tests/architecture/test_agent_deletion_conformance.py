from __future__ import annotations

import re
from pathlib import Path


def test_agent_deletion_entrypoints_must_use_soft_delete() -> None:
    root = Path(__file__).resolve().parents[2] / "app"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if (
            re.search(r"\bdb\.delete\(\s*agent\s*[),]", text)
            or re.search(r"\bdelete\(\s*Agent\s*[),]", text)
            or re.search(r"\bsql_delete\(\s*Agent\s*[),]", text)
        ):
            offenders.append(path.relative_to(root).as_posix())

    assert offenders == []
