from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def test_company_knowledge_uses_native_authority_without_retired_provider_residue() -> None:
    files_source = (APP_ROOT / "api" / "files.py").read_text(encoding="utf-8")
    invoker_source = (APP_ROOT / "runtime" / "invoker.py").read_text(encoding="utf-8")
    tool_runtime_source = (APP_ROOT / "tools" / "service.py").read_text(encoding="utf-8")

    assert (APP_ROOT / "models" / "company_knowledge.py").exists()
    assert (APP_ROOT / "models" / "company_ontology.py").exists()
    assert (APP_ROOT / "services" / "company_knowledge_permissions.py").exists()
    assert (APP_ROOT / "services" / "company_knowledge_evidence.py").exists()
    assert "enterprise_kb_router" not in files_source
    assert "/enterprise/knowledge-base" not in files_source
    assert not (APP_ROOT / "services" / "truth_search_service.py").exists()
    assert not (APP_ROOT / "services" / "viking_client.py").exists()
    assert "TruthSearchService" not in invoker_source
    assert "viking_client" not in invoker_source
    assert "truth_search_service" not in tool_runtime_source


def test_company_context_and_legacy_export_are_separate_surfaces() -> None:
    enterprise_source = (APP_ROOT / "api" / "enterprise.py").read_text(encoding="utf-8")
    workspace_source = (APP_ROOT / "services" / "agent_tool_domains" / "workspace.py").read_text(encoding="utf-8")

    assert '"/legacy-company-files/status"' in enterprise_source
    assert '"/legacy-company-files/export"' in enterprise_source
    assert "scan_legacy_company_files" in enterprise_source
    assert "company_context_path_allowed" in workspace_source
    assert "enterprise_info/knowledge_base" not in workspace_source
