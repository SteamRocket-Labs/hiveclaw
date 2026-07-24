from __future__ import annotations

import copy
from importlib import resources
import uuid
import zipfile

import pytest


def test_builtin_domain_pack_catalog_has_two_versioned_signed_products() -> None:
    from app.services.company_ontology_contracts import load_builtin_ontology_catalog

    catalog = load_builtin_ontology_catalog()

    assert catalog.package_keys == (
        "policy-agent-action-approval",
        "project-goal-deliverable-owner",
    )
    for package_key in catalog.package_keys:
        assert catalog.versions(package_key) == ("1.0.0", "1.1.0")
        for version in catalog.versions(package_key):
            bundle = catalog.get(package_key, version)
            assert bundle is not None
            assert bundle.manifest.package_key == package_key
            assert bundle.manifest.version == version
            assert bundle.signature.algorithm == "ed25519"
            assert bundle.signature.key_ref == "hive_builtin_ontology_2026_07"
            assert len(bundle.content_hash) == 64
            assert bundle.verification_receipt["signature_valid"] is True
            assert bundle.verification_receipt["contract_compatible"] is True
            assert bundle.acceptance.golden_queries
            assert bundle.acceptance.golden_actions
            assert bundle.migrations.rollback_compatible is True


def test_builtin_domain_pack_catalog_loads_from_wheel_style_traversable(tmp_path) -> None:
    from app.services.company_ontology_contracts import load_builtin_ontology_catalog

    source_root = resources.files("app.ontology").joinpath("domain_packs")
    archive_path = tmp_path / "ontology-assets.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for package_dir in source_root.iterdir():
            if not package_dir.is_dir():
                continue
            for version_file in package_dir.iterdir():
                if version_file.name.endswith(".json"):
                    archive.writestr(
                        f"domain_packs/{package_dir.name}/{version_file.name}",
                        version_file.read_bytes(),
                    )

    with zipfile.ZipFile(archive_path) as archive:
        catalog = load_builtin_ontology_catalog(
            root=zipfile.Path(archive, at="domain_packs/"),
        )

    assert catalog.package_keys == (
        "policy-agent-action-approval",
        "project-goal-deliverable-owner",
    )
    assert len(catalog.all()) == 4
    assert all(bundle.verification_receipt["signature_valid"] for bundle in catalog.all())


def test_domain_pack_signature_and_declarative_boundary_fail_closed_on_tamper() -> None:
    from app.services.company_ontology_contracts import (
        OntologyPackageRejected,
        load_builtin_ontology_catalog,
        verify_ontology_package_payload,
    )

    bundle = load_builtin_ontology_catalog().get("policy-agent-action-approval", "1.0.0")
    assert bundle is not None
    payload = copy.deepcopy(bundle.raw_payload)
    payload["schema"]["object_types"][0]["display_name"] = "Tampered"

    with pytest.raises(OntologyPackageRejected, match="signature"):
        verify_ontology_package_payload(payload)

    executable = copy.deepcopy(bundle.raw_payload)
    executable["engine"] = {"python_import": "evil.module:run"}
    with pytest.raises(OntologyPackageRejected, match="declarative"):
        verify_ontology_package_payload(executable)


def test_candidate_contract_preserves_model_semantics_but_requires_complete_evidence() -> None:
    from app.services.company_ontology_contracts import (
        OntologyCandidatePatch,
        OntologyCandidateRejected,
        validate_ontology_candidate,
    )

    candidate = OntologyCandidatePatch.model_validate(
        {
            "schema_version": "hive.company_ontology_candidate.v1",
            "snapshot_complete": True,
            "objects": [
                {
                    "stable_object_key": "project:apollo",
                    "object_type_ref": "project.Project",
                    "display_name": "Apollo",
                    "properties": {"project.name": "Apollo"},
                    "source_refs": ["company-evidence://00000000-0000-0000-0000-000000000001"],
                    "source_identities": [
                        {
                            "source_contract_id": "00000000-0000-0000-0000-000000000002",
                            "source_identity_key": "apollo",
                            "aliases": ["Apollo"],
                        }
                    ],
                    "sensitivity": "PL2_pii",
                    "permission_resource_ref": "object:project:apollo",
                    "valid_from": "2026-07-24T00:00:00Z",
                    "observed_at": "2026-07-24T00:00:00Z",
                }
            ],
            "assertions": [
                {
                    "stable_assertion_key": "project:apollo:name",
                    "subject_key": "project:apollo",
                    "predicate_ref": "project.name",
                    "typed_value": "Apollo",
                    "assertion_kind": "sourced",
                    "source_refs": ["company-evidence://00000000-0000-0000-0000-000000000001"],
                    "sensitivity": "PL2_pii",
                    "permission_resource_ref": "assertion:project:apollo:name",
                    "valid_from": "2026-07-24T00:00:00Z",
                    "observed_at": "2026-07-24T00:00:00Z",
                }
            ],
            "links": [],
            "events": [],
            "definition_overrides": {},
            "coverage_ledger": {
                "complete": True,
                "total_units": 1,
                "covered_units": 1,
                "missing_units": [],
            },
            "conflict_ledger": {"unresolved": []},
            "unresolved_questions": [],
            "model_prompt_receipts": [
                {
                    "model": "provider/model",
                    "prompt_hash": "a" * 64,
                    "response_hash": "b" * 64,
                    "source_refs": ["company-evidence://00000000-0000-0000-0000-000000000001"],
                }
            ],
        }
    )

    validated = validate_ontology_candidate(candidate)
    assert validated.model_dump(mode="json")["objects"][0]["display_name"] == "Apollo"

    incomplete = candidate.model_copy(
        update={
            "coverage_ledger": candidate.coverage_ledger.model_copy(
                update={
                    "complete": False,
                    "total_units": 2,
                    "covered_units": 1,
                    "missing_units": ["unit-2"],
                }
            )
        }
    )
    with pytest.raises(OntologyCandidateRejected, match="coverage"):
        validate_ontology_candidate(incomplete)


def test_runtime_model_receipt_replaces_model_supplied_audit_claims() -> None:
    from app.services.company_ontology_contracts import (
        bind_runtime_model_receipt,
        ontology_semantic_candidate_hash,
    )

    agent_id = "00000000-0000-0000-0000-000000000009"
    candidate = {
        "schema_version": "hive.company_ontology_candidate.v1",
        "snapshot_complete": True,
        "objects": [
            {
                "stable_object_key": "project:apollo",
                "object_type_ref": "project.Project",
                "display_name": "Apollo",
                "properties": {"project.name": "Apollo"},
                "source_refs": ["company-evidence://00000000-0000-0000-0000-000000000001"],
                "source_identities": [],
                "sensitivity": "PL2_pii",
                "permission_resource_ref": "object:project:apollo",
                "valid_from": "2026-07-24T00:00:00Z",
                "observed_at": "2026-07-24T00:00:00Z",
            }
        ],
        "assertions": [],
        "links": [],
        "events": [],
        "definition_overrides": {},
        "coverage_ledger": {
            "complete": True,
            "total_units": 1,
            "covered_units": 1,
            "missing_units": [],
        },
        "conflict_ledger": {"unresolved": []},
        "unresolved_questions": [],
        "model_prompt_receipts": [
            {
                "model": "forged/model",
                "prompt_hash": "f" * 64,
                "response_hash": "e" * 64,
                "source_refs": ["company-evidence://00000000-0000-0000-0000-000000000003"],
            }
        ],
    }
    semantic_payload = {**candidate, "model_prompt_receipts": []}

    bound = bind_runtime_model_receipt(
        candidate,
        runtime_receipt={
            "schema": "hive.company_ontology_model_execution.v1",
            "receipt_source": "tool_runtime",
            "agent_id": agent_id,
            "turn_id": "turn-ontology-1",
            "runtime_task_id": None,
            "model": "provider/trusted-model",
            "prompt_hash": "a" * 64,
        },
    )

    assert len(bound.model_prompt_receipts) == 1
    receipt = bound.model_prompt_receipts[0]
    assert receipt.model == "provider/trusted-model"
    assert receipt.prompt_hash == "a" * 64
    assert receipt.response_hash == ontology_semantic_candidate_hash(semantic_payload)
    assert receipt.source_refs == ["company-evidence://00000000-0000-0000-0000-000000000001"]
    assert receipt.receipt_source == "tool_runtime"
    assert str(receipt.agent_id) == agent_id
    assert receipt.turn_id == "turn-ontology-1"
    assert ontology_semantic_candidate_hash(candidate) == ontology_semantic_candidate_hash(
        {
            **candidate,
            "model_prompt_receipts": [
                {
                    "model": "another/runtime",
                    "prompt_hash": "1" * 64,
                    "response_hash": "2" * 64,
                    "source_refs": candidate["objects"][0]["source_refs"],
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_reference_engine_is_typed_side_effect_free_and_provider_replaceable() -> None:
    from app.services.company_ontology_contracts import (
        OntologyCandidatePatch,
        load_builtin_ontology_catalog,
    )
    from app.services.company_ontology_engine import ReferenceOntologyEngine

    bundle = load_builtin_ontology_catalog().get("policy-agent-action-approval", "1.0.0")
    assert bundle is not None
    engine = ReferenceOntologyEngine()

    status = await engine.capability_status()
    validation = await engine.validate_package(bundle)
    query = await engine.query(
        {
            "query_ref": bundle.queries[0].query_ref,
            "objects": [],
            "object_type_refs": list(bundle.queries[0].object_type_refs),
            "limit": bundle.queries[0].max_items,
        }
    )
    simulation = await engine.simulate_action(
        action_definition=bundle.actions[0],
        proposed_input={"agent_ref": "agent:finance", "action_ref": "action:send-payment"},
    )
    invalid_candidate = OntologyCandidatePatch.model_validate(
        {
            "schema_version": "hive.company_ontology_candidate.v1",
            "snapshot_complete": True,
            "objects": [
                {
                    "stable_object_key": "policy:invalid",
                    "object_type_ref": "policy.AgentActionPolicy",
                    "display_name": "Invalid policy",
                    "properties": {
                        "policy.agent_ref": "agent:finance",
                        "policy.action_ref": "action:send-payment",
                        "policy.mode": True,
                    },
                    "source_refs": ["company-evidence://00000000-0000-0000-0000-000000000001"],
                    "sensitivity": "PL1_public",
                    "permission_resource_ref": "object:policy:invalid",
                    "valid_from": "2026-07-24T00:00:00Z",
                    "observed_at": "2026-07-24T00:00:00Z",
                }
            ],
            "coverage_ledger": {
                "complete": True,
                "total_units": 1,
                "covered_units": 1,
                "missing_units": [],
            },
            "conflict_ledger": {"unresolved": []},
        }
    )
    invalid_validation = await engine.validate_candidate(
        package=bundle,
        candidate=invalid_candidate,
    )

    assert status == {
        "status": "available",
        "provider": "hive_reference",
        "capabilities": [
            "validate_package",
            "validate_candidate",
            "materialize_release_projection",
            "query",
            "resolve_fact_lineage",
            "simulate_action",
            "rebuild_projection",
        ],
        "authority": "hive_postgresql",
        "derived_projection": "rebuildable",
    }
    assert validation["passed"] is True
    assert query["contract"]["typed_only"] is True
    assert query["contract"]["natural_language_hard_gate"] is False
    assert query["contract"]["relation_expansion_authorized_first"] is True
    assert invalid_validation["passed"] is False
    assert any("policy.mode has invalid string value" in error for error in invalid_validation["errors"])
    assert simulation["status"] == "simulated"
    assert simulation["effect_committed"] is False
    assert simulation["external_side_effects"] == []


@pytest.mark.asyncio
async def test_curation_service_rejects_user_or_mismatched_agent_receipt_before_database_access() -> None:
    from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal
    from app.services.company_ontology_service import (
        CompanyOntologyService,
        OntologyCurationRequest,
    )

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    request = OntologyCurationRequest(
        activation_id=uuid.uuid4(),
        baseline_release_id=None,
        source_contract_versions=({"source_contract_id": str(uuid.uuid4()), "version": 1},),
        evidence_scope={},
        requested_operations=("extract",),
        candidate_patch={},
        idempotency_key="curation-boundary",
        trace_id="curation-boundary",
        model_execution_receipt={
            "schema": "hive.company_ontology_model_execution.v1",
            "receipt_source": "tool_runtime",
            "agent_id": str(uuid.uuid4()),
            "turn_id": "turn-boundary",
            "model": "provider/model",
            "prompt_hash": "a" * 64,
        },
    )
    service = CompanyOntologyService()

    with pytest.raises(PermissionError, match="agent_runtime_curation_required"):
        await service.start_curation(
            None,
            principal=CompanyKnowledgePrincipal(
                tenant_id=tenant_id,
                accountable_user_id=user_id,
                accountable_role="org_admin",
                actor_type="user",
                actor_id=user_id,
                purpose="interactive_session",
                session_id="session-boundary",
            ),
            request=request,
        )

    with pytest.raises(PermissionError, match="model_receipt_agent_mismatch"):
        await service.start_curation(
            None,
            principal=CompanyKnowledgePrincipal(
                tenant_id=tenant_id,
                accountable_user_id=user_id,
                accountable_role="org_admin",
                actor_type="agent",
                actor_id=agent_id,
                purpose="interactive_session",
                session_id="session-boundary",
            ),
            request=request,
        )
