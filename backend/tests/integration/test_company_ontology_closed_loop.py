from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import uuid

import pytest
from sqlalchemy import func, select

from app.models import import_all_models
from app.models.agent import Agent
from app.models.company_knowledge import (
    CompanyKnowledgeEvent,
    CompanyKnowledgeOutbox,
)
from app.models.company_ontology import (
    CompanyOntologyAssertion,
    CompanyOntologyEvidenceBinding,
    CompanyOntologyObject,
    CompanyOntologyRelease,
    CompanyOntologyReleaseItem,
)
from app.models.security_audit import ResourcePermission
from app.models.tenant import Tenant
from app.models.user import User
from app.services.company_knowledge_contracts import SourceContractInput
from app.services.company_knowledge_evidence import verify_company_knowledge_event_chain
from app.services.company_knowledge_gateway import CompanyKnowledgeGateway
from app.services.company_knowledge_indexer import CompanyKnowledgeIndexer
from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal
from app.services.company_knowledge_service import (
    CompanyEvidenceIngestRequest,
    CompanyKnowledgeProposalRequest,
    CompanyKnowledgeReviewRequest,
    CompanyKnowledgeService,
)
from app.services.company_ontology_gateway import (
    CompanyOntologyGateway,
    OntologyActionSimulationRequest,
    OntologyFactExplainRequest,
    OntologyQueryRequest,
)
from app.services.company_ontology_contracts import (
    OntologyPackageCatalog,
    OntologyPackageDependency,
    load_builtin_ontology_catalog,
)
from app.services.company_ontology_engine import ReferenceOntologyEngine
from app.services.company_ontology_service import (
    CompanyOntologyService,
    OntologyActivationRequest,
    OntologyCurationRequest,
    OntologyPackageInstallRequest,
    OntologyReleaseLifecycleRequest,
)


def _principal(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "org_admin",
) -> CompanyKnowledgePrincipal:
    return CompanyKnowledgePrincipal(
        tenant_id=tenant_id,
        accountable_user_id=user_id,
        accountable_role=role,
        actor_type="user",
        actor_id=user_id,
        purpose="interactive_session",
        session_id="company-ontology-integration",
    )


def _source_contract() -> SourceContractInput:
    return SourceContractInput(
        source_kind="document",
        provider_kind="native",
        stable_source_id="agent-action-policy",
        owner_principal_ref="role:org_admin",
        accountable_steward_ref="role:org_admin",
        connection_ref=None,
        schema_ref="schema://policy-document/v1",
        schema_version="1",
        identity_keys=("source_item_id",),
        relation_keys=(),
        ingest_mode="manual",
        cursor_kind=None,
        cursor_policy={},
        watermark_field=None,
        temporal_mapping={"observed_at": "ingest_time"},
        source_acl_mapping_policy={"mode": "required_snapshot"},
        default_sensitivity="PL2_pii",
        export_policy={"allowed": False},
        retention_policy={"class": "company_record"},
        legal_hold_policy={"supported": True},
        allowed_namespaces=("policy",),
        precedence_policy_ref=None,
        acceptance_suite_ref="acceptance://policy-document/v1",
        idempotency_policy={"key": "source_item_id+revision"},
    )


class _RecoveringOntologyEngine(ReferenceOntologyEngine):
    def __init__(self) -> None:
        self.candidate_attempts = 0

    async def validate_candidate(self, **kwargs):
        self.candidate_attempts += 1
        if self.candidate_attempts == 1:
            raise RuntimeError("simulated ontology engine outage")
        return await super().validate_candidate(**kwargs)


class _ActivationRecoveringOntologyEngine(ReferenceOntologyEngine):
    def __init__(self) -> None:
        self.package_attempts = 0

    async def validate_package(self, package):
        self.package_attempts += 1
        if self.package_attempts == 1:
            raise RuntimeError("simulated activation engine outage")
        return await super().validate_package(package)


@pytest.mark.asyncio
async def test_company_ontology_install_curate_review_publish_query_recover_and_restore(
    owner_sessionmaker,
    tmp_path: Path,
) -> None:
    import_all_models()
    tenant_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    curator_agent_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()
    partial_id = uuid.uuid4()
    denied_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    creator = _principal(tenant_id=tenant_id, user_id=creator_id)
    curator = CompanyKnowledgePrincipal(
        tenant_id=tenant_id,
        accountable_user_id=creator_id,
        accountable_role="org_admin",
        actor_type="agent",
        actor_id=curator_agent_id,
        purpose="interactive_session",
        session_id="company-ontology-integration",
    )
    reviewer = _principal(tenant_id=tenant_id, user_id=reviewer_id)
    partial = _principal(tenant_id=tenant_id, user_id=partial_id)
    denied = _principal(tenant_id=tenant_id, user_id=denied_id)
    knowledge = CompanyKnowledgeService(data_root=tmp_path)
    engine = _RecoveringOntologyEngine()
    ontology = CompanyOntologyService(knowledge_service=knowledge, engine=engine)

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Ontology", slug=f"ontology-{tenant_id.hex[:10]}"))
        for user_id, label in (
            (creator_id, "Creator"),
            (reviewer_id, "Reviewer"),
            (partial_id, "Partial"),
            (denied_id, "Denied"),
        ):
            db.add(
                User(
                    id=user_id,
                    username=f"ontology-{label.lower()}-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:10]}@ontology.test",
                    password_hash="x",
                    display_name=f"Ontology {label}",
                    role="org_admin",
                    tenant_id=tenant_id,
                )
            )
        await db.flush()
        db.add(
            Agent(
                id=curator_agent_id,
                tenant_id=tenant_id,
                name="Ontology Curator",
                role_description="Curates governed ontology candidates",
                creator_id=creator_id,
                owner_user_id=creator_id,
                status="idle",
            )
        )
        await db.flush()
        db.add(
            ResourcePermission(
                tenant_id=tenant_id,
                principal_type="user",
                principal_id=creator_id,
                resource_type="company_knowledge_scope",
                resource_id=tenant_id,
                actions=[
                    "discover",
                    "read",
                    "propose",
                    "publish",
                    "retire",
                    "restore",
                    "install_package",
                    "activate_package",
                    "curate",
                    "query",
                    "simulate",
                ],
                conditions={},
                effect="allow",
                sensitivity_ceiling="PL3_sensitive",
                purposes=["interactive_session"],
                created_by_user_id=creator_id,
            )
        )
        db.add(
            ResourcePermission(
                tenant_id=tenant_id,
                principal_type="user",
                principal_id=reviewer_id,
                resource_type="company_knowledge_scope",
                resource_id=tenant_id,
                actions=["discover", "read", "review", "approve"],
                conditions={},
                effect="allow",
                sensitivity_ceiling="PL3_sensitive",
                purposes=["interactive_session"],
                created_by_user_id=creator_id,
            )
        )
        for resource_type, resource_key in (
            ("company_knowledge_namespace", "namespace:policy"),
            ("company_ontology_object", "object:policy:finance-send-payment"),
            ("company_ontology_assertion", "assertion:policy:finance-send-payment:mode"),
            ("company_ontology_assertion", "assertion:policy:finance-send-payment:target"),
        ):
            db.add(
                ResourcePermission(
                    tenant_id=tenant_id,
                    principal_type="user",
                    principal_id=partial_id,
                    resource_type=resource_type,
                    resource_key=resource_key,
                    actions=["query"],
                    conditions={},
                    effect="allow",
                    sensitivity_ceiling="PL3_sensitive",
                    purposes=["interactive_session"],
                    created_by_user_id=creator_id,
                )
            )
        db.add(
            ResourcePermission(
                tenant_id=tenant_id,
                principal_type="user",
                principal_id=partial_id,
                resource_type="company_ontology_object",
                resource_key="object:policy:restricted-target",
                actions=["query"],
                conditions={},
                effect="deny",
                sensitivity_ceiling="PL3_sensitive",
                purposes=["interactive_session"],
                created_by_user_id=creator_id,
            )
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        contract = await knowledge.register_source_contract(
            db,
            principal=creator,
            contract_input=_source_contract(),
            idempotency_key="ontology-contract:v1",
            trace_id="ontology-contract",
        )
        job = await knowledge.queue_evidence_import(
            db,
            principal=creator,
            request=CompanyEvidenceIngestRequest(
                source_contract_id=contract.id,
                source_contract_version=1,
                evidence_kind="document",
                source_item_id="agent-action-policy",
                source_revision="2026-07-24",
                title="Agent action policy",
                markdown=("# Agent action policy\n\nFinance Agent must request confirmation before sending a payment."),
                typed_payload=None,
                external_artifact_ref=None,
                schema_ref="schema://policy-document/v1",
                source_acl_snapshot={"role_names": ["org_admin"]},
                proposed_namespace="policy",
                proposed_sensitivity="PL2_pii",
                occurred_at=None,
                effective_from=now,
                effective_until=None,
                observed_at=now,
                cursor={},
                sequence=None,
                coverage_ledger={
                    "complete": True,
                    "total_units": 1,
                    "covered_units": 1,
                    "missing_units": [],
                },
                purpose="curate typed action policy",
                idempotency_key="ontology-import:v1",
                trace_id="ontology-import",
            ),
        )
        await db.commit()
    processed = await knowledge.process_import_job(
        tenant_id=tenant_id,
        job_id=job.id,
        session_factory=owner_sessionmaker,
    )
    assert processed.status == "completed"
    assert processed.evidence_id is not None
    evidence_ref = f"company-evidence://{processed.evidence_id}"

    async with owner_sessionmaker() as db:
        catalog = load_builtin_ontology_catalog()
        policy_bundle = catalog.get("policy-agent-action-approval", "1.0.0")
        assert policy_bundle is not None
        missing_dependency_bundle = replace(
            policy_bundle,
            manifest=policy_bundle.manifest.model_copy(
                update={
                    "dependencies": [
                        OntologyPackageDependency(
                            package_key="missing-foundation",
                            version="1.0.0",
                        )
                    ]
                }
            ),
        )
        with (
            patch(
                "app.services.company_ontology_service.load_builtin_ontology_catalog",
                return_value=OntologyPackageCatalog([missing_dependency_bundle]),
            ),
            pytest.raises(ValueError, match="company_ontology_package_dependency_missing"),
        ):
            await ontology.install_package(
                db,
                principal=creator,
                request=OntologyPackageInstallRequest(
                    package_key="policy-agent-action-approval",
                    version="1.0.0",
                    idempotency_key="ontology-install:missing-dependency",
                    trace_id="ontology-install",
                ),
            )

        installation = await ontology.install_package(
            db,
            principal=creator,
            request=OntologyPackageInstallRequest(
                package_key="policy-agent-action-approval",
                version="1.0.0",
                idempotency_key="ontology-install:policy:1.0.0",
                trace_id="ontology-install",
            ),
        )
        project_bundle = catalog.get("project-goal-deliverable-owner", "1.0.0")
        assert project_bundle is not None
        conflicting_bundle = replace(
            project_bundle,
            manifest=project_bundle.manifest.model_copy(
                update={
                    "conflicts": [
                        OntologyPackageDependency(
                            package_key="policy-agent-action-approval",
                            version="1.0.0",
                        )
                    ]
                }
            ),
        )
        with (
            patch(
                "app.services.company_ontology_service.load_builtin_ontology_catalog",
                return_value=OntologyPackageCatalog([conflicting_bundle]),
            ),
            pytest.raises(ValueError, match="company_ontology_package_conflict"),
        ):
            await ontology.install_package(
                db,
                principal=creator,
                request=OntologyPackageInstallRequest(
                    package_key="project-goal-deliverable-owner",
                    version="1.0.0",
                    idempotency_key="ontology-install:conflict",
                    trace_id="ontology-install",
                ),
            )
        activation = await ontology.create_activation(
            db,
            principal=creator,
            request=OntologyActivationRequest(
                installation_id=installation.id,
                namespace="policy",
                configuration={},
                idempotency_key="ontology-activation:policy:v1",
                trace_id="ontology-activation",
            ),
        )
        replayed_activation = await ontology.create_activation(
            db,
            principal=creator,
            request=OntologyActivationRequest(
                installation_id=installation.id,
                namespace="policy",
                configuration={},
                idempotency_key="ontology-activation:policy:v1",
                trace_id="ontology-activation-replay",
            ),
        )
        assert replayed_activation.id == activation.id
        recovery_service = CompanyOntologyService(
            knowledge_service=knowledge,
            engine=_ActivationRecoveringOntologyEngine(),
        )
        blocked_activation = await recovery_service.dry_run_activation(
            db,
            principal=creator,
            activation_id=activation.id,
            idempotency_key="ontology-dry-run:policy:v1:attempt-1",
            trace_id="ontology-dry-run",
        )
        await db.commit()
        assert blocked_activation.status == "blocked"
        assert blocked_activation.dry_run_receipt_json["passed"] is False
        assert blocked_activation.dry_run_receipt_json["retryable"] is True

    async with owner_sessionmaker() as db:
        activation = await ontology.dry_run_activation(
            db,
            principal=creator,
            activation_id=activation.id,
            idempotency_key="ontology-dry-run:policy:v1:attempt-2",
            trace_id="ontology-dry-run-retry",
        )
        await db.commit()
        assert activation.status == "active"
        assert activation.dry_run_receipt_json["passed"] is True
        receipt = activation.dry_run_receipt_json
        assert all(item["passed"] for item in receipt["golden_queries"])
        assert all(item["passed"] for item in receipt["golden_actions"])
        assert all(item["passed"] for item in receipt["acl_cases"])
        assert all(item["passed"] for item in receipt["conflict_cases"])
        assert all(item["passed"] for item in receipt["temporal_cases"])
        replayed_dry_run = await ontology.dry_run_activation(
            db,
            principal=creator,
            activation_id=activation.id,
            idempotency_key="ontology-dry-run:policy:v1:attempt-2",
            trace_id="ontology-dry-run-retry-replayed",
        )
        assert replayed_dry_run.id == activation.id
        assert replayed_dry_run.dry_run_receipt_json == receipt

    candidate = {
        "schema_version": "hive.company_ontology_candidate.v1",
        "snapshot_complete": True,
        "objects": [
            {
                "stable_object_key": "policy:finance-send-payment",
                "object_type_ref": "policy.AgentActionPolicy",
                "display_name": "Finance payment policy",
                "properties": {
                    "policy.agent_ref": "agent:finance",
                    "policy.action_ref": "action:send-payment",
                    "policy.mode": "confirm_first",
                },
                "source_refs": [evidence_ref],
                "source_identities": [
                    {
                        "source_contract_id": str(contract.id),
                        "source_identity_key": "finance-send-payment",
                        "aliases": ["Finance payment policy"],
                    }
                ],
                "sensitivity": "PL2_pii",
                "permission_resource_ref": "object:policy:finance-send-payment",
                "valid_from": now.isoformat(),
                "observed_at": now.isoformat(),
            },
            {
                "stable_object_key": "policy:restricted-target",
                "object_type_ref": "policy.AgentActionPolicy",
                "display_name": "Restricted target policy",
                "properties": {
                    "policy.agent_ref": "agent:restricted",
                    "policy.action_ref": "action:restricted",
                    "policy.mode": "never_do",
                },
                "source_refs": [evidence_ref],
                "source_identities": [],
                "sensitivity": "PL2_pii",
                "permission_resource_ref": "object:policy:restricted-target",
                "valid_from": now.isoformat(),
                "observed_at": now.isoformat(),
            },
        ],
        "assertions": [
            {
                "stable_assertion_key": "policy:finance-send-payment:mode",
                "subject_key": "policy:finance-send-payment",
                "predicate_ref": "policy.mode",
                "typed_value": "confirm_first",
                "assertion_kind": "sourced",
                "source_refs": [evidence_ref],
                "sensitivity": "PL2_pii",
                "permission_resource_ref": "assertion:policy:finance-send-payment:mode",
                "valid_from": now.isoformat(),
                "observed_at": now.isoformat(),
            },
            {
                "stable_assertion_key": "policy:finance-send-payment:target",
                "subject_key": "policy:finance-send-payment",
                "predicate_ref": "policy.related_policy",
                "object_key": "policy:restricted-target",
                "assertion_kind": "sourced",
                "source_refs": [evidence_ref],
                "sensitivity": "PL2_pii",
                "permission_resource_ref": "assertion:policy:finance-send-payment:target",
                "valid_from": now.isoformat(),
                "observed_at": now.isoformat(),
            },
        ],
        "links": [],
        "events": [],
        "definition_overrides": {
            "property_types": [
                {
                    "property_ref": "policy.related_policy",
                    "display_name": "Related policy",
                    "description": "Evidence-backed relation to another policy object.",
                    "value_type": "object_ref",
                    "object_type_refs": ["policy.AgentActionPolicy"],
                    "cardinality": "one",
                    "required": False,
                    "source_refs": [evidence_ref],
                    "sensitivity": "PL2_pii",
                    "permission_resource_ref": "definition:policy.related_policy",
                }
            ]
        },
        "coverage_ledger": {
            "complete": True,
            "total_units": 1,
            "covered_units": 1,
            "missing_units": [],
        },
        "conflict_ledger": {"unresolved": [], "resolved": []},
        "unresolved_questions": [],
        "model_prompt_receipts": [
            {
                "model": "provider/model",
                "prompt_hash": "a" * 64,
                "response_hash": "b" * 64,
                "source_refs": [evidence_ref],
            }
        ],
    }
    async with owner_sessionmaker() as db:
        curation_request = OntologyCurationRequest(
            activation_id=activation.id,
            baseline_release_id=None,
            source_contract_versions=(
                {
                    "source_contract_id": str(contract.id),
                    "version": contract.version,
                },
            ),
            evidence_scope={"evidence_refs": [evidence_ref]},
            requested_operations=("create",),
            candidate_patch=candidate,
            idempotency_key="ontology-curation:policy:v1",
            trace_id="ontology-curation",
            model_execution_receipt={
                "schema": "hive.company_ontology_model_execution.v1",
                "receipt_source": "tool_runtime",
                "agent_id": str(curator_agent_id),
                "turn_id": "turn-ontology-integration",
                "runtime_task_id": None,
                "model": "provider/model",
                "prompt_hash": "a" * 64,
            },
        )
        held = await ontology.start_curation(
            db,
            principal=curator,
            request=curation_request,
        )
        await db.commit()
        assert held.run.status == "held"
        assert held.run.retry_state_json == {"attempt": 1, "retryable": True}
        assert held.run.checkpoint_ref == (f"company-ontology-checkpoint://{held.run.id}/candidate-preserved")
        assert held.proposal is None

    async with owner_sessionmaker() as db:
        result = await ontology.start_curation(
            db,
            principal=curator,
            request=curation_request,
        )
        await db.commit()
        assert result.run.status == "completed"
        assert result.run.retry_state_json == {"attempt": 2, "retryable": False}
        assert result.run.checkpoint_ref == (f"company-ontology-checkpoint://{result.run.id}/proposal-submitted")
        assert result.run.candidate_patch_json["objects"][0]["display_name"] == ("Finance payment policy")
        assert result.run.model_prompt_receipts_json[0]["receipt_source"] == "tool_runtime"
        assert result.run.model_prompt_receipts_json[0]["agent_id"] == str(curator_agent_id)
        assert result.run.model_prompt_receipts_json[0]["model"] == "provider/model"
        assert result.proposal is not None
        assert result.proposal.status == "submitted"
        proposal_id = result.proposal.id
        run_id = result.run.id

    async with owner_sessionmaker() as db:
        proposal = await db.get(type(result.proposal), proposal_id)
        assert proposal is not None
        with pytest.raises(PermissionError, match="reviewer_role_mismatch"):
            await knowledge.record_review(
                db,
                principal=reviewer,
                proposal_id=proposal.id,
                request=CompanyKnowledgeReviewRequest(
                    decision="approve",
                    reviewer_role="security",
                    reason="Attempted role spoof.",
                    evidence_refs=(evidence_ref,),
                    policy_snapshot={"policy": "ontology-review-v1"},
                ),
                expected_state_version=proposal.state_version,
                trace_id="ontology-review-spoof",
            )
        reviewed = await knowledge.record_review(
            db,
            principal=reviewer,
            proposal_id=proposal.id,
            request=CompanyKnowledgeReviewRequest(
                decision="approve",
                reviewer_role="org_admin",
                reason="Typed definitions, evidence, ACL, and acceptance receipts reviewed.",
                evidence_refs=(evidence_ref,),
                policy_snapshot={"policy": "ontology-review-v1"},
            ),
            expected_state_version=proposal.state_version,
            trace_id="ontology-review",
        )
        await db.commit()
        assert reviewed.status == "approved"

    async with owner_sessionmaker() as db:
        release = await ontology.publish_curation_run(
            db,
            principal=creator,
            run_id=run_id,
            valid_from=now,
            valid_until=None,
            trace_id="ontology-publish",
        )
        await db.commit()
        assert release.version == 1
        assert release.status == "active"
        assert release.release_hash
        release_id = release.id

    async with owner_sessionmaker() as db:
        replayed_release = await ontology.publish_curation_run(
            db,
            principal=creator,
            run_id=run_id,
            valid_from=now,
            valid_until=None,
            trace_id="ontology-publish-replay",
        )
        await db.commit()
        assert replayed_release.id == release_id

    gateway = CompanyOntologyGateway()
    async with owner_sessionmaker() as db:
        queried = await gateway.query(
            db,
            principal=creator,
            request=OntologyQueryRequest(
                namespaces=("policy",),
                query_ref="policy.effective_action_policy",
                query_input={},
                object_type_refs=("policy.AgentActionPolicy",),
                limit=10,
                trace_id="ontology-query",
            ),
        )
        assert queried.status == "ok"
        assert queried.payload["result_count"] == 2
        assert queried.payload["objects"][0]["properties"]["policy.mode"] == ("confirm_first")
        assert len(queried.payload["objects"][0]["facts"]) == 2
        mode_fact = next(
            fact for fact in queried.payload["objects"][0]["facts"] if fact["predicate_ref"] == "policy.mode"
        )
        assert mode_fact["sensitivity"] == "PL2_pii"
        assert mode_fact["source_refs"] == [evidence_ref]
        assertion_id = uuid.UUID(mode_fact["assertion_id"])

        explanation = await gateway.explain_fact(
            db,
            principal=creator,
            request=OntologyFactExplainRequest(
                assertion_id=assertion_id,
                trace_id="ontology-explain",
            ),
        )
        assert explanation.status == "ok"
        assert explanation.payload["fact"]["sensitivity"] == "PL2_pii"
        assert explanation.payload["fact"]["source_refs"] == [evidence_ref]
        assert explanation.payload["fact"]["lineage"]["coverage"]["complete"] is True
        assert explanation.payload["fact"]["lineage"]["evidence"][0]["evidence_ref"] == evidence_ref

        simulation = await gateway.simulate_action(
            db,
            principal=creator,
            request=OntologyActionSimulationRequest(
                action_type_ref="policy.request_agent_action",
                proposed_input={
                    "agent_ref": "agent:finance",
                    "action_ref": "action:send-payment",
                },
                namespace="policy",
                trace_id="ontology-simulate",
            ),
        )
        assert simulation.status == "simulated"
        assert simulation.payload["simulation"]["effect_committed"] is False
        assert simulation.payload["simulation"]["external_side_effects"] == []

        partial_query = await gateway.query(
            db,
            principal=partial,
            request=OntologyQueryRequest(
                namespaces=("policy",),
                object_type_refs=("policy.AgentActionPolicy",),
                limit=10,
                trace_id="ontology-partial-query",
            ),
        )
        assert partial_query.status == "ok"
        assert partial_query.payload["result_count"] == 1
        assert partial_query.payload["objects"][0]["display_name"] == "Finance payment policy"
        assert all(fact["object_id"] is None for fact in partial_query.payload["objects"][0]["facts"])
        assert "Restricted target policy" not in str(partial_query.as_dict())
        partial_types = await gateway.list_types(
            db,
            principal=partial,
            namespaces=("policy",),
            trace_id="ontology-partial-types",
        )
        assert partial_types.status == "ok"
        assert {item["type_ref"] for item in partial_types.payload["types"]} == {
            "policy.ActionRequest",
            "policy.AgentActionPolicy",
        }

        denied_query = await gateway.query(
            db,
            principal=denied,
            request=OntologyQueryRequest(
                namespaces=("policy",),
                limit=10,
                trace_id="ontology-denied-query",
            ),
        )
        assert denied_query.status == "empty"
        assert denied_query.payload["objects"] == []
        assert "Finance payment policy" not in str(denied_query.as_dict())
        denied_types = await gateway.list_types(
            db,
            principal=denied,
            namespaces=("policy",),
            trace_id="ontology-denied-types",
        )
        assert denied_types.status == "empty"
        assert denied_types.payload["types"] == []
        await db.commit()

    indexer = CompanyKnowledgeIndexer()
    summary = await indexer.process_pending(
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
        limit=20,
    )
    assert summary.failed == 0
    assert summary.completed >= 2

    async with owner_sessionmaker() as db:
        retired = await ontology.retire_release(
            db,
            principal=creator,
            release_id=release_id,
            request=OntologyReleaseLifecycleRequest(
                reason="Verify immutable restoration.",
                trace_id="ontology-retire",
            ),
        )
        replayed_retirement = await ontology.retire_release(
            db,
            principal=creator,
            release_id=release_id,
            request=OntologyReleaseLifecycleRequest(
                reason="Verify immutable restoration.",
                trace_id="ontology-retire-replay",
            ),
        )
        assert replayed_retirement.id == retired.id
        restore_proposal = await knowledge.create_proposal(
            db,
            principal=creator,
            request=CompanyKnowledgeProposalRequest(
                proposal_kind="ontology",
                source_id=None,
                source_document_id=None,
                source_revision_ref=f"company-ontology-release://{retired.id}",
                baseline_publication_id=None,
                baseline_version=None,
                proposed_patch={
                    "operation": "restore_ontology_release",
                    "release_id": str(retired.id),
                    "reason": "The reviewed policy remains valid.",
                },
                proposed_namespace="policy",
                proposed_sensitivity="PL2_pii",
                source_refs=(evidence_ref,),
                source_coverage={
                    "complete": True,
                    "total_units": 1,
                    "covered_units": 1,
                    "missing_units": [],
                },
                conflict_candidates=(),
                ontology_mapping={"restore_release_id": str(retired.id)},
                risk_level="high",
                required_review_policy={
                    "minimum_approvals": 1,
                    "required_roles": ["org_admin"],
                    "separation": True,
                },
                idempotency_key="ontology-restore-proposal:v1",
                trace_id="ontology-restore-proposal",
            ),
        )
        restore_proposal = await knowledge.submit_proposal(
            db,
            principal=creator,
            proposal_id=restore_proposal.id,
            expected_state_version=restore_proposal.state_version,
            trace_id="ontology-restore-submit",
        )
        restore_proposal = await knowledge.record_review(
            db,
            principal=reviewer,
            proposal_id=restore_proposal.id,
            request=CompanyKnowledgeReviewRequest(
                decision="approve",
                reviewer_role="org_admin",
                reason="Restoration lineage and unchanged evidence were reviewed.",
                evidence_refs=(evidence_ref,),
                policy_snapshot={"policy": "ontology-restore-v1"},
            ),
            expected_state_version=restore_proposal.state_version,
            trace_id="ontology-restore-review",
        )
        restored = await ontology.restore_release(
            db,
            principal=creator,
            release_id=retired.id,
            request=OntologyReleaseLifecycleRequest(
                reason="Restore reviewed release.",
                trace_id="ontology-restore",
                approved_proposal_id=restore_proposal.id,
                valid_from=now,
            ),
        )
        await db.commit()
        assert retired.status == "retired"
        assert restored.id != retired.id
        assert restored.version == 2
        assert restored.status == "active"
        assert restored.restored_from_release_id == retired.id
        replayed_restore = await ontology.restore_release(
            db,
            principal=creator,
            release_id=retired.id,
            request=OntologyReleaseLifecycleRequest(
                reason="Restore reviewed release.",
                trace_id="ontology-restore-replay",
                approved_proposal_id=restore_proposal.id,
                valid_from=now,
            ),
        )
        await db.commit()
        assert replayed_restore.id == restored.id

        installation_v11 = await ontology.install_package(
            db,
            principal=creator,
            request=OntologyPackageInstallRequest(
                package_key="policy-agent-action-approval",
                version="1.1.0",
                idempotency_key="ontology-install:policy:1.1.0",
                trace_id="ontology-install-v11",
            ),
        )
        await db.commit()

        async def _create_parallel_activation(
            installation_id: uuid.UUID,
            *,
            key: str,
        ):
            async with owner_sessionmaker() as parallel_db:
                created = await ontology.create_activation(
                    parallel_db,
                    principal=creator,
                    request=OntologyActivationRequest(
                        installation_id=installation_id,
                        namespace="policy",
                        configuration={},
                        idempotency_key=key,
                        trace_id=key,
                    ),
                )
                await parallel_db.commit()
                return created

        parallel_activations = await asyncio.gather(
            _create_parallel_activation(
                installation.id,
                key="ontology-activation:policy:parallel-v10",
            ),
            _create_parallel_activation(
                installation_v11.id,
                key="ontology-activation:policy:parallel-v11",
            ),
        )
        assert sorted(item.activation_version for item in parallel_activations) == [2, 3]

        assert (
            await db.scalar(
                select(func.count(CompanyOntologyRelease.id)).where(
                    CompanyOntologyRelease.tenant_id == tenant_id,
                    CompanyOntologyRelease.namespace == "policy",
                    CompanyOntologyRelease.status == "active",
                )
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count(CompanyOntologyObject.id)).where(CompanyOntologyObject.tenant_id == tenant_id)
            )
            == 4
        )
        assert (
            await db.scalar(
                select(func.count(CompanyOntologyAssertion.id)).where(CompanyOntologyAssertion.tenant_id == tenant_id)
            )
            == 4
        )
        assert (
            await db.scalar(
                select(func.count(CompanyOntologyReleaseItem.id)).where(
                    CompanyOntologyReleaseItem.tenant_id == tenant_id
                )
            )
            > 2
        )
        assert (
            await db.scalar(
                select(func.count(CompanyOntologyEvidenceBinding.id)).where(
                    CompanyOntologyEvidenceBinding.tenant_id == tenant_id
                )
            )
            == 8
        )
        events = (
            (
                await db.execute(
                    select(CompanyKnowledgeEvent)
                    .where(CompanyKnowledgeEvent.tenant_id == tenant_id)
                    .order_by(CompanyKnowledgeEvent.stream_sequence)
                )
            )
            .scalars()
            .all()
        )
        assert verify_company_knowledge_event_chain(list(events))["valid"] is True
        assert {
            "company_ontology.package_installed",
            "company_ontology.activation_dry_run_failed",
            "company_ontology.activation_dry_run_passed",
            "company_ontology.curation_started",
            "company_ontology.curation_held",
            "company_ontology.curation_retried",
            "company_ontology.curation_completed",
            "company_ontology.release_published",
            "company_ontology.release_retired",
            "company_knowledge.permission_allowed",
            "company_knowledge.permission_denied",
        } <= {event.event_type for event in events}
        assert (
            await db.scalar(
                select(func.count(CompanyKnowledgeOutbox.id)).where(
                    CompanyKnowledgeOutbox.tenant_id == tenant_id,
                    CompanyKnowledgeOutbox.status == "pending",
                )
            )
            >= 2
        )

    second_summary = await indexer.process_pending(
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
        limit=20,
    )
    assert second_summary.failed == 0
    assert second_summary.completed >= 2

    # Company Ontology remains independent from the Company document gateway.
    assert isinstance(CompanyKnowledgeGateway(), CompanyKnowledgeGateway)
