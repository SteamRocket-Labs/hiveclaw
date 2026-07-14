from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_onlyoffice_runtime_and_infrastructure_surfaces_are_retired():
    checked_paths = [
        ROOT / "backend" / "app",
        ROOT / "frontend" / "src",
        ROOT / ".env.example",
        ROOT / "docker-compose.yml",
        ROOT / "deploy",
    ]
    forbidden = ("ONLYOFFICE", "OnlyOfficeHost", "DocsAPI", "onlyoffice-documentserver")
    findings = []
    for checked in checked_paths:
        paths = checked.rglob("*") if checked.is_dir() else [checked]
        for path in paths:
            if not path.is_file() or path.suffix in {".pyc", ".map"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for token in forbidden:
                if token in text:
                    findings.append(f"{path.relative_to(ROOT)} contains {token}")

    assert findings == []
    assert not (ROOT / "deploy" / "onlyoffice-documentserver").exists()
