from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"


def test_ontology_engine_is_replaceable_and_cannot_own_hive_authority() -> None:
    source = (APP_ROOT / "services" / "company_ontology_engine.py").read_text(encoding="utf-8")

    assert "class OntologyEnginePlugin(Protocol)" in source
    assert "class ReferenceOntologyEngine" in source
    assert "app.database" not in source
    assert "app.models" not in source
    assert "ResourcePermission" not in source
    assert "CompanyOntologyRelease(" not in source
    assert "ToolRuntimeService" not in source


def test_domain_packs_are_declarative_assets_not_executable_plugins() -> None:
    pack_root = APP_ROOT / "ontology" / "domain_packs"
    assets = sorted(pack_root.glob("*/*.json"))

    assert len(assets) == 4
    assert not list(pack_root.rglob("*.py"))
    for asset in assets:
        source = asset.read_text(encoding="utf-8")
        assert '"signature"' in source
        assert '"acceptance"' in source
        assert "python_import" not in source
        assert "shell_command" not in source
        assert "executable" not in source


def test_ontology_service_and_gateway_keep_authority_in_hive_core() -> None:
    service = (APP_ROOT / "services" / "company_ontology_service.py").read_text(encoding="utf-8")
    gateway = (APP_ROOT / "services" / "company_ontology_gateway.py").read_text(encoding="utf-8")
    tools = (APP_ROOT / "tools" / "handlers" / "company_ontology.py").read_text(encoding="utf-8")

    assert "resolve_company_knowledge_permission" in service
    assert "append_company_knowledge_event_with_outbox" in service
    assert ".commit(" not in service
    assert "resolve_company_knowledge_permission" in gateway
    assert "company_knowledge.permission_denied" in gateway
    assert "company_knowledge.permission_allowed" in gateway
    assert "ToolRuntimeService" not in gateway
    assert "install_company_ontology_package" not in tools
    assert "publish_company_ontology_release" not in tools
    assert "manage_company_ontology_permissions" not in tools
