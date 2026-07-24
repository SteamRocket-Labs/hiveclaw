from __future__ import annotations

from sqlalchemy import inspect

from app.models.company_knowledge import (
    CompanyKnowledgeEvent,
    CompanyKnowledgeEvidence,
    CompanyKnowledgeOutbox,
    CompanyKnowledgeProposal,
    CompanyKnowledgePublication,
    CompanyKnowledgeReview,
    CompanyKnowledgeSource,
    CompanyKnowledgeSourceContract,
)
from app.models.company_ontology import (
    CompanyOntologyActionType,
    CompanyOntologyActivation,
    CompanyOntologyAssertion,
    CompanyOntologyCurationRun,
    CompanyOntologyEvidenceBinding,
    CompanyOntologyEvent,
    CompanyOntologyEventType,
    CompanyOntologyLink,
    CompanyOntologyLinkType,
    CompanyOntologyObject,
    CompanyOntologyObjectIdentity,
    CompanyOntologyObjectType,
    CompanyOntologyPackage,
    CompanyOntologyPackageInstallation,
    CompanyOntologyPackageVersion,
    CompanyOntologyPropertyType,
    CompanyOntologyRelease,
    CompanyOntologyReleaseItem,
    CompanyOntologyRuleDefinition,
)
from app.models.security_audit import ResourcePermission


def test_company_knowledge_authority_models_are_distinct_from_personal_knowledge() -> None:
    expected_tables = {
        CompanyKnowledgeSourceContract: "company_knowledge_source_contracts",
        CompanyKnowledgeSource: "company_knowledge_sources",
        CompanyKnowledgeEvidence: "company_knowledge_evidence",
        CompanyKnowledgeProposal: "company_knowledge_proposals",
        CompanyKnowledgeReview: "company_knowledge_reviews",
        CompanyKnowledgePublication: "company_knowledge_publications",
        CompanyKnowledgeEvent: "company_knowledge_events",
        CompanyKnowledgeOutbox: "company_knowledge_outbox",
    }

    assert {model.__tablename__ for model in expected_tables} == set(expected_tables.values())
    for model, table_name in expected_tables.items():
        mapper = inspect(model)
        assert mapper.local_table.name == table_name
        assert {"id", "tenant_id", "created_at"} <= {column.key for column in mapper.columns}

    assert {"source_acl_snapshot_hash", "content_hash", "canonical_envelope_json"} <= {
        column.key for column in inspect(CompanyKnowledgeEvidence).columns
    }
    assert {"review_set_hash", "rollback_ref", "supersedes_publication_id"} <= {
        column.key for column in inspect(CompanyKnowledgePublication).columns
    }
    assert {"idempotency_key", "available_at", "attempt_count", "last_error"} <= {
        column.key for column in inspect(CompanyKnowledgeOutbox).columns
    }


def test_company_ontology_models_keep_release_authority_separate_from_knowledge_publications() -> None:
    expected_tables = {
        CompanyOntologyPackage: "company_ontology_packages",
        CompanyOntologyPackageVersion: "company_ontology_package_versions",
        CompanyOntologyPackageInstallation: "company_ontology_package_installations",
        CompanyOntologyActivation: "company_ontology_activations",
        CompanyOntologyCurationRun: "company_ontology_curation_runs",
        CompanyOntologyRelease: "company_ontology_releases",
        CompanyOntologyObjectType: "company_ontology_object_types",
        CompanyOntologyPropertyType: "company_ontology_property_types",
        CompanyOntologyLinkType: "company_ontology_link_types",
        CompanyOntologyEventType: "company_ontology_event_types",
        CompanyOntologyRuleDefinition: "company_ontology_rule_definitions",
        CompanyOntologyActionType: "company_ontology_action_types",
        CompanyOntologyObject: "company_ontology_objects",
        CompanyOntologyObjectIdentity: "company_ontology_object_identities",
        CompanyOntologyAssertion: "company_ontology_assertions",
        CompanyOntologyLink: "company_ontology_links",
        CompanyOntologyEvent: "company_ontology_events",
        CompanyOntologyEvidenceBinding: "company_ontology_evidence_bindings",
        CompanyOntologyReleaseItem: "company_ontology_release_items",
    }

    assert {model.__tablename__ for model in expected_tables} == set(expected_tables.values())
    for model, table_name in expected_tables.items():
        mapper = inspect(model)
        assert mapper.local_table.name == table_name
        assert {"id", "tenant_id", "created_at"} <= {column.key for column in mapper.columns}

    assertion_columns = {column.key for column in inspect(CompanyOntologyAssertion).columns}
    assert {
        "assertion_kind",
        "typed_value_json",
        "valid_from",
        "valid_until",
        "observed_at",
        "derived_by_rule_ref",
        "curation_run_id",
        "release_id",
        "sensitivity",
        "permission_resource_ref",
        "status",
    } <= assertion_columns
    assert "publication_id" not in {column.key for column in inspect(CompanyOntologyRelease).columns}


def test_resource_permission_supports_company_deny_expiry_and_runtime_binding_contract() -> None:
    columns = {column.key: column for column in inspect(ResourcePermission).columns}

    assert {
        "principal_key",
        "resource_key",
        "effect",
        "sensitivity_ceiling",
        "purposes",
        "source_acl_snapshot_hash",
        "expires_at",
        "revoked_at",
        "created_by_user_id",
        "revoked_by_user_id",
    } <= set(columns)
    assert columns["principal_id"].nullable is True
    assert columns["resource_id"].nullable is True
