from __future__ import annotations

import re
from pathlib import Path


def test_agent_tools_import_allowlist_is_down_to_facade_contract_tests() -> None:
    project_root = Path(__file__).resolve().parents[3]
    pattern = re.compile(
        r"^\s*(from app\.services\.agent_tools import|import app\.services\.agent_tools|from app\.services import agent_tools)\b",
        re.MULTILINE,
    )

    importers = sorted(
        str(path.relative_to(project_root))
        for path in (project_root / "backend").rglob("*.py")
        if path != project_root / "backend/app/services/agent_tools.py"
        and pattern.search(path.read_text(encoding="utf-8"))
    )

    assert importers == []
