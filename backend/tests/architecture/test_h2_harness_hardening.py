from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def test_tool_runtime_backend_contract_exists_and_service_uses_it() -> None:
    backend_source = (APP_ROOT / "tools" / "backends.py").read_text(encoding="utf-8")
    service_source = (APP_ROOT / "tools" / "service.py").read_text(encoding="utf-8")

    assert "class ToolRuntimeBackend" in backend_source
    assert "class LocalToolRuntimeBackend" in backend_source
    assert "class DockerToolRuntimeBackend" in backend_source
    assert "backend:" in service_source
    assert ".backend.execute(" in service_source


def test_external_skill_import_surfaces_use_skill_guard() -> None:
    skill_api_source = (APP_ROOT / "api" / "skills.py").read_text(encoding="utf-8")
    files_api_source = (APP_ROOT / "api" / "files.py").read_text(encoding="utf-8")
    hr_source = (APP_ROOT / "tools" / "handlers" / "hr.py").read_text(encoding="utf-8")
    skill_tool_source = (APP_ROOT / "tools" / "handlers" / "skills.py").read_text(encoding="utf-8")

    for source in (skill_api_source, files_api_source, hr_source, skill_tool_source):
        assert "skill_guard" in source or "scan_skill_files" in source

