from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import inspect

from app.models.company_ontology import (
    CompanyOntologyActivation,
    CompanyOntologyAssertion,
    CompanyOntologyCurationRun,
    CompanyOntologyLink,
)


MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "company_ontology_runtime_0724.py"


def test_company_ontology_runtime_migration_preserves_release_versioned_identity() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location("company_ontology_runtime_0724", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "company_ontology_runtime_0724"
    assert module.down_revision == "company_knowledge_runtime_0724"
    assert "candidate_patch_json" in source
    assert "uq_company_ontology_activation_idempotency" in source
    assert '["tenant_id", "namespace", "activation_version"]' in source
    assert "stable_assertion_key" in source
    assert "stable_link_key" in source
    assert "uq_company_ontology_object_release_key" in source
    assert "uq_company_ontology_assertion_release_key" in source
    assert "uq_company_ontology_link_release_key" in source
    assert "uq_company_ontology_event_release_key" in source
    assert "uq_company_ontology_source_identity_object" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "current_setting('app.current_tenant_id', true) = 'BYPASS'" in source
    assert "app.rls_bypass" not in source


def test_company_ontology_runtime_models_expose_exact_model_candidate_and_stable_keys() -> None:
    assert "idempotency_key" in {column.key for column in inspect(CompanyOntologyActivation).columns}
    assert "candidate_patch_json" in {column.key for column in inspect(CompanyOntologyCurationRun).columns}
    assert "stable_assertion_key" in {column.key for column in inspect(CompanyOntologyAssertion).columns}
    assert "stable_link_key" in {column.key for column in inspect(CompanyOntologyLink).columns}
