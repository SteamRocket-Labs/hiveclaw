"""Hive-authoritative Company Ontology install, curation, and release service.

The replaceable engine validates typed inputs and builds derived projections.
This service alone owns tenant authority, review state, immutable releases,
evidence bindings, lifecycle events, and transactional outbox work.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from app.models.company_knowledge import (
    CompanyKnowledgeEvidence,
    CompanyKnowledgeProposal,
    CompanyKnowledgeReview,
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
from app.models.runtime_task import RuntimeTask
from app.models.security_audit import ResourcePermission
from app.services.company_knowledge_contracts import (
    evaluate_company_review_set,
    next_company_proposal_status,
)
from app.services.company_knowledge_evidence import (
    CompanyKnowledgeEventInput,
    append_company_knowledge_event,
    append_company_knowledge_event_with_outbox,
)
from app.services.company_knowledge_permissions import (
    CompanyKnowledgePrincipal,
    CompanyKnowledgeResource,
    resolve_company_knowledge_permission,
)
from app.services.company_knowledge_service import (
    CompanyKnowledgeProposalRequest,
    CompanyKnowledgeService,
)
from app.services.company_ontology_contracts import (
    OntologyCandidatePatch,
    OntologyCandidateRejected,
    OntologyPackageBundle,
    bind_runtime_model_receipt,
    load_builtin_ontology_catalog,
    ontology_candidate_hash,
    ontology_semantic_candidate_hash,
    validate_ontology_candidate,
)
from app.services.company_ontology_engine import (
    OntologyEngineUnavailable,
    OntologyEnginePlugin,
    ReferenceOntologyEngine,
    validate_typed_payload,
)
from app.services.privacy_layer import canonicalize_sensitivity, sensitivity_rank


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _hash(value: Any) -> str:
    rendered = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _evidence_id(source_ref: str) -> uuid.UUID:
    rendered = str(source_ref or "").strip()
    if not rendered.startswith("company-evidence://"):
        raise ValueError("company_ontology_evidence_ref_required")
    try:
        return uuid.UUID(rendered.removeprefix("company-evidence://").split("#", 1)[0])
    except ValueError as exc:
        raise ValueError("company_ontology_evidence_ref_invalid") from exc


async def _lock_ontology_scope(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    scope: str,
) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"company-ontology:{tenant_id}:{scope}"},
    )


def _event_input(
    *,
    principal: CompanyKnowledgePrincipal,
    event_type: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    resource_version: int | None,
    source_refs: tuple[str, ...],
    source_hash: str | None,
    policy_snapshot: dict[str, Any],
    trace_id: str,
    idempotency_key: str,
    outcome: str,
    payload: dict[str, Any],
) -> CompanyKnowledgeEventInput:
    return CompanyKnowledgeEventInput(
        tenant_id=principal.tenant_id,
        event_type=event_type,
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        accountable_user_id=principal.accountable_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_version=resource_version,
        source_refs=source_refs,
        source_hash=source_hash,
        policy_snapshot=policy_snapshot,
        trace_id=trace_id,
        request_id=None,
        idempotency_key=idempotency_key,
        outcome=outcome,
        payload=payload,
        occurred_at=_utcnow(),
    )


@dataclass(frozen=True, slots=True)
class OntologyPackageInstallRequest:
    package_key: str
    version: str
    idempotency_key: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class OntologyActivationRequest:
    installation_id: uuid.UUID
    namespace: str
    configuration: dict[str, Any]
    idempotency_key: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class OntologyCurationRequest:
    activation_id: uuid.UUID
    baseline_release_id: uuid.UUID | None
    source_contract_versions: tuple[dict[str, Any], ...]
    evidence_scope: dict[str, Any]
    requested_operations: tuple[str, ...]
    candidate_patch: dict[str, Any]
    idempotency_key: str
    trace_id: str
    model_execution_receipt: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class OntologyReleaseLifecycleRequest:
    reason: str
    trace_id: str
    approved_proposal_id: uuid.UUID | None = None
    valid_from: datetime | None = None


@dataclass(frozen=True, slots=True)
class OntologyCurationResult:
    run: CompanyOntologyCurationRun
    proposal: CompanyKnowledgeProposal | None


class CompanyOntologyService:
    """One transaction-scoped authority service; callers own transaction commit."""

    def __init__(
        self,
        *,
        engine: OntologyEnginePlugin | None = None,
        knowledge_service: CompanyKnowledgeService | None = None,
    ) -> None:
        self._engine = engine or ReferenceOntologyEngine()
        self._knowledge_service = knowledge_service or CompanyKnowledgeService(data_root=Path("."))

    @staticmethod
    async def _require_permission(
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        namespace: str,
        action: str,
        resource_type: str = "company_ontology_namespace",
        resource_id: uuid.UUID | None = None,
        resource_key: str | None = None,
        sensitivity: str = "PL1_public",
        source_acl_snapshot_hash: str | None = None,
        source_acl: dict[str, Any] | None = None,
        evidence_access_complete: bool = True,
        publication_status: str | None = None,
        validity_active: bool = True,
    ) -> dict[str, Any]:
        decision = await resolve_company_knowledge_permission(
            session,
            principal=principal,
            resource=CompanyKnowledgeResource(
                tenant_id=principal.tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_key=resource_key or f"namespace:{namespace}",
                namespace=namespace,
                sensitivity=sensitivity,
                source_acl_snapshot_hash=source_acl_snapshot_hash,
                source_acl=source_acl,
                evidence_access_complete=evidence_access_complete,
                publication_status=publication_status,
                validity_active=validity_active,
            ),
            action=action,
        )
        if not decision.allowed:
            raise PermissionError(decision.deny_reason_code or "company_ontology_permission_denied")
        return decision.evidence()

    async def install_package(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: OntologyPackageInstallRequest,
    ) -> CompanyOntologyPackageInstallation:
        bundle = load_builtin_ontology_catalog().get(
            request.package_key.strip(),
            request.version.strip(),
        )
        if bundle is None:
            raise LookupError("company_ontology_package_not_found")
        namespace = bundle.manifest.namespaces[0]
        policy = await self._require_permission(
            session,
            principal=principal,
            namespace=namespace,
            action="install_package",
        )
        await _lock_ontology_scope(
            session,
            tenant_id=principal.tenant_id,
            scope=f"package:{bundle.manifest.package_key}",
        )
        await self._validate_installation_graph(
            session,
            tenant_id=principal.tenant_id,
            bundle=bundle,
        )
        try:
            engine_receipt = await self._engine.validate_package(bundle)
        except Exception as exc:
            raise OntologyEngineUnavailable("company_ontology_engine_unavailable") from exc
        if engine_receipt.get("passed") is not True:
            raise ValueError("company_ontology_package_engine_validation_failed")

        package = (
            await session.execute(
                select(CompanyOntologyPackage)
                .where(
                    CompanyOntologyPackage.tenant_id == principal.tenant_id,
                    CompanyOntologyPackage.package_key == bundle.manifest.package_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if package is None:
            package = CompanyOntologyPackage(
                tenant_id=principal.tenant_id,
                package_key=bundle.manifest.package_key,
                display_name=bundle.manifest.display_name,
                publisher=bundle.manifest.publisher,
                description=bundle.manifest.description,
                status="available",
            )
            session.add(package)
            await session.flush()

        package_version = (
            await session.execute(
                select(CompanyOntologyPackageVersion).where(
                    CompanyOntologyPackageVersion.tenant_id == principal.tenant_id,
                    CompanyOntologyPackageVersion.package_id == package.id,
                    CompanyOntologyPackageVersion.version == bundle.manifest.version,
                )
            )
        ).scalar_one_or_none()
        if package_version is None:
            package_version = CompanyOntologyPackageVersion(
                tenant_id=principal.tenant_id,
                package_id=package.id,
                version=bundle.manifest.version,
                content_hash=bundle.content_hash,
                signature=bundle.signature.value,
                signature_key_ref=bundle.signature.key_ref,
                hive_contract_version=bundle.manifest.hive_contract_version,
                engine_capabilities_json=list(bundle.manifest.engine_capabilities),
                namespaces_json=list(bundle.manifest.namespaces),
                dependencies_json=[item.model_dump(mode="json") for item in bundle.manifest.dependencies],
                conflicts_json=[item.model_dump(mode="json") for item in bundle.manifest.conflicts],
                manifest_json=bundle.manifest.model_dump(mode="json"),
                schema_json=bundle.schema.model_dump(mode="json"),
                mappings_json=dict(bundle.mappings),
                rules_json=[item.model_dump(mode="json") for item in bundle.rules],
                queries_json=[item.model_dump(mode="json") for item in bundle.queries],
                actions_json=[item.model_dump(mode="json") for item in bundle.actions],
                permissions_json=dict(bundle.permissions),
                acceptance_json=bundle.acceptance.model_dump(mode="json"),
                migrations_json=bundle.migrations.model_dump(mode="json"),
                admission_status="admitted",
                admission_receipt_json={
                    **bundle.verification_receipt,
                    "engine_validation": engine_receipt,
                },
            )
            session.add(package_version)
            await session.flush()
        elif package_version.content_hash != bundle.content_hash:
            raise ValueError("company_ontology_package_version_hash_conflict")

        installation = (
            await session.execute(
                select(CompanyOntologyPackageInstallation)
                .where(
                    CompanyOntologyPackageInstallation.tenant_id == principal.tenant_id,
                    CompanyOntologyPackageInstallation.package_version_id == package_version.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if installation is not None:
            return installation
        installation = CompanyOntologyPackageInstallation(
            tenant_id=principal.tenant_id,
            package_version_id=package_version.id,
            status="installed",
            requested_capabilities_json=list(bundle.manifest.engine_capabilities),
            compatibility_receipt_json={
                "package_admission": bundle.verification_receipt,
                "engine_validation": engine_receipt,
            },
            acceptance_receipt_json={},
            installed_by_user_id=principal.accountable_user_id,
            installed_at=_utcnow(),
        )
        session.add(installation)
        await session.flush()
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_ontology.package_installed",
                resource_type="ontology_package_installation",
                resource_id=installation.id,
                resource_version=None,
                source_refs=(f"ontology-package://{bundle.content_hash}",),
                source_hash=bundle.content_hash,
                policy_snapshot=policy,
                trace_id=request.trace_id,
                idempotency_key=f"{request.idempotency_key}:installed",
                outcome="installed",
                payload={
                    "package_key": bundle.manifest.package_key,
                    "version": bundle.manifest.version,
                    "namespace": namespace,
                },
            ),
        )
        return installation

    async def create_activation(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: OntologyActivationRequest,
    ) -> CompanyOntologyActivation:
        namespace = request.namespace.strip()
        await _lock_ontology_scope(
            session,
            tenant_id=principal.tenant_id,
            scope=f"namespace:{namespace}",
        )
        installation, package_version, bundle = await self._installation_bundle(
            session,
            tenant_id=principal.tenant_id,
            installation_id=request.installation_id,
            for_update=True,
        )
        if namespace not in set(bundle.manifest.namespaces):
            raise ValueError("company_ontology_namespace_not_declared_by_package")
        policy = await self._require_permission(
            session,
            principal=principal,
            namespace=namespace,
            action="activate_package",
        )
        if installation.status != "installed" or package_version.admission_status != "admitted":
            raise ValueError("company_ontology_installation_not_activatable")
        idempotency_key = request.idempotency_key.strip()
        if not 1 <= len(idempotency_key) <= 300:
            raise ValueError("company_ontology_activation_idempotency_key_required")
        existing = (
            await session.execute(
                select(CompanyOntologyActivation)
                .where(
                    CompanyOntologyActivation.tenant_id == principal.tenant_id,
                    CompanyOntologyActivation.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.installation_id != installation.id
                or existing.namespace != namespace
                or _hash(existing.configuration_json) != _hash(dict(request.configuration))
            ):
                raise ValueError("company_ontology_activation_idempotency_conflict")
            return existing
        version = (
            int(
                await session.scalar(
                    select(func.coalesce(func.max(CompanyOntologyActivation.activation_version), 0)).where(
                        CompanyOntologyActivation.tenant_id == principal.tenant_id,
                        CompanyOntologyActivation.namespace == namespace,
                    )
                )
                or 0
            )
            + 1
        )
        activation = CompanyOntologyActivation(
            tenant_id=principal.tenant_id,
            installation_id=installation.id,
            namespace=namespace,
            activation_version=version,
            idempotency_key=idempotency_key,
            configuration_json=dict(request.configuration),
            dry_run_receipt_json={},
            status="draft",
        )
        session.add(activation)
        await session.flush()
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_ontology.activation_created",
                resource_type="ontology_activation",
                resource_id=activation.id,
                resource_version=activation.activation_version,
                source_refs=(f"ontology-package://{bundle.content_hash}",),
                source_hash=bundle.content_hash,
                policy_snapshot=policy,
                trace_id=request.trace_id,
                idempotency_key=f"{idempotency_key}:created",
                outcome="draft",
                payload={"namespace": namespace, "status": "draft"},
            ),
        )
        return activation

    async def dry_run_activation(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        activation_id: uuid.UUID,
        idempotency_key: str,
        trace_id: str,
    ) -> CompanyOntologyActivation:
        activation = (
            await session.execute(
                select(CompanyOntologyActivation).where(
                    CompanyOntologyActivation.id == activation_id,
                    CompanyOntologyActivation.tenant_id == principal.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if activation is None:
            raise LookupError("company_ontology_activation_not_found")
        await _lock_ontology_scope(
            session,
            tenant_id=principal.tenant_id,
            scope=f"namespace:{activation.namespace}",
        )
        activation = (
            await session.execute(
                select(CompanyOntologyActivation)
                .where(
                    CompanyOntologyActivation.id == activation_id,
                    CompanyOntologyActivation.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        installation, _package_version, bundle = await self._installation_bundle(
            session,
            tenant_id=principal.tenant_id,
            installation_id=activation.installation_id,
        )
        policy = await self._require_permission(
            session,
            principal=principal,
            namespace=activation.namespace,
            action="activate_package",
        )
        request_key = idempotency_key.strip()
        if not 1 <= len(request_key) <= 300:
            raise ValueError("company_ontology_activation_dry_run_idempotency_key_required")
        previous_receipt = dict(activation.dry_run_receipt_json or {})
        if previous_receipt.get("idempotency_key") == request_key:
            return activation
        attempt = max(0, int(previous_receipt.get("attempt") or 0)) + 1
        engine_unavailable = False
        try:
            package_validation = await self._engine.validate_package(bundle)
        except Exception:  # noqa: BLE001 - provider details never enter the receipt
            engine_unavailable = True
            package_validation = {
                "schema": "hive.company_ontology_engine_validation.v1",
                "passed": False,
                "status": "unavailable",
                "error_code": "company_ontology_engine_unavailable",
            }

        queries = {item.query_ref: item for item in bundle.queries}
        query_results: list[dict[str, Any]] = []
        if not engine_unavailable:
            for case in bundle.acceptance.golden_queries:
                query_definition = queries[case.query_ref]
                input_errors = validate_typed_payload(
                    query_definition.input_schema,
                    dict(case.input),
                )
                try:
                    result = await self._engine.query(
                        {
                            "query_ref": case.query_ref,
                            "query_input": dict(case.input),
                            "objects": [],
                            "object_type_refs": list(query_definition.object_type_refs),
                            "object_ids": list(case.input.get("object_ids") or []),
                            "limit": query_definition.max_items,
                        }
                    )
                except Exception:  # noqa: BLE001 - typed provider outage
                    engine_unavailable = True
                    result = {
                        "status": "unavailable",
                        "error_code": "company_ontology_engine_unavailable",
                    }
                expected = dict(case.expected_contract)
                observed_contract = dict(result.get("contract") or {})
                output_errors = validate_typed_payload(
                    query_definition.result_schema,
                    result,
                )
                query_results.append(
                    {
                        "case_ref": case.case_ref,
                        "query_ref": case.query_ref,
                        "passed": (
                            not input_errors
                            and not output_errors
                            and all(observed_contract.get(key) == value for key, value in expected.items())
                        ),
                        "input_validation_errors": list(input_errors),
                        "output_validation_errors": list(output_errors),
                        "observed_contract": observed_contract,
                        "expected_contract": expected,
                        "engine_status": result.get("status"),
                    }
                )

        action_results: list[dict[str, Any]] = []
        actions = {item.action_type_ref: item for item in bundle.actions}
        if not engine_unavailable:
            for case in bundle.acceptance.golden_actions:
                try:
                    result = await self._engine.simulate_action(
                        action_definition=actions[case.action_type_ref],
                        proposed_input=dict(case.input),
                    )
                except Exception:  # noqa: BLE001 - typed provider outage
                    engine_unavailable = True
                    result = {
                        "status": "unavailable",
                        "error_code": "company_ontology_engine_unavailable",
                    }
                expected = dict(case.expected_contract)
                passed = bool(result.get("input_valid")) and all(
                    result.get(key) == value for key, value in expected.items()
                )
                action_results.append(
                    {
                        "case_ref": case.case_ref,
                        "action_type_ref": case.action_type_ref,
                        "passed": passed,
                        "observed_contract": {key: result.get(key) for key in expected},
                        "expected_contract": expected,
                        "engine_status": result.get("status"),
                    }
                )

        acl_results: list[dict[str, Any]] = []
        for case in bundle.acceptance.acl_cases:
            role = case.principal_ref.removeprefix("role:") if case.principal_ref.startswith("role:") else ""
            if not role:
                acl_results.append(
                    {
                        "case_ref": case.case_ref,
                        "passed": False,
                        "observed": "unsupported_principal",
                        "expected": case.expected,
                    }
                )
                continue
            acceptance_user_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"hive:{principal.tenant_id}:{bundle.content_hash}:{case.case_ref}",
            )
            acceptance_principal = CompanyKnowledgePrincipal(
                tenant_id=principal.tenant_id,
                accountable_user_id=acceptance_user_id,
                accountable_role=role,
                actor_type="user",
                actor_id=acceptance_user_id,
                purpose="ontology_acceptance",
            )
            try:
                decision = await resolve_company_knowledge_permission(
                    session,
                    principal=acceptance_principal,
                    resource=CompanyKnowledgeResource(
                        tenant_id=principal.tenant_id,
                        resource_type="company_ontology_namespace",
                        resource_id=None,
                        resource_key=case.resource_ref,
                        namespace=activation.namespace,
                        sensitivity=str(bundle.permissions.get("default_sensitivity") or "PL1_public"),
                        source_acl_snapshot_hash=bundle.content_hash,
                        source_acl={"all_tenant_members": True},
                        evidence_access_complete=True,
                        publication_status="active",
                        validity_active=True,
                    ),
                    action=case.action,
                )
                observed = "allow" if decision.allowed else "deny"
                deny_reason = decision.deny_reason_code
            except ValueError as exc:
                observed = "unsupported_action"
                deny_reason = str(exc)
            acl_results.append(
                {
                    "case_ref": case.case_ref,
                    "passed": observed == case.expected,
                    "observed": observed,
                    "expected": case.expected,
                    "deny_reason_code": deny_reason,
                }
            )

        conflict_results: list[dict[str, Any]] = []
        for case in bundle.acceptance.conflict_cases:
            expected = str(case.get("expected") or "")
            observed = "unsupported"
            if expected == "deny":
                role = f"ontology_acceptance_{_hash(case)[:16]}"
                nested = await session.begin_nested()
                try:
                    for effect in ("allow", "deny"):
                        session.add(
                            ResourcePermission(
                                tenant_id=principal.tenant_id,
                                principal_type="role",
                                principal_id=None,
                                principal_key=f"role:{role}",
                                resource_type="company_knowledge_namespace",
                                resource_id=None,
                                resource_key=f"namespace:{activation.namespace}",
                                actions=["query"],
                                conditions={},
                                effect=effect,
                                sensitivity_ceiling="PL4_credential",
                                purposes=["ontology_acceptance"],
                                source_acl_snapshot_hash=bundle.content_hash,
                                created_by_user_id=principal.accountable_user_id,
                            )
                        )
                    await session.flush()
                    synthetic_id = uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"hive:{principal.tenant_id}:{bundle.content_hash}:{case.get('case_ref')}:conflict",
                    )
                    decision = await resolve_company_knowledge_permission(
                        session,
                        principal=CompanyKnowledgePrincipal(
                            tenant_id=principal.tenant_id,
                            accountable_user_id=synthetic_id,
                            accountable_role=role,
                            actor_type="user",
                            actor_id=synthetic_id,
                            purpose="ontology_acceptance",
                        ),
                        resource=CompanyKnowledgeResource(
                            tenant_id=principal.tenant_id,
                            resource_type="company_ontology_namespace",
                            resource_id=None,
                            resource_key=f"namespace:{activation.namespace}",
                            namespace=activation.namespace,
                            sensitivity="PL1_public",
                            source_acl_snapshot_hash=bundle.content_hash,
                            source_acl={"all_tenant_members": True},
                            evidence_access_complete=True,
                            publication_status="active",
                            validity_active=True,
                        ),
                        action="query",
                    )
                    if not decision.allowed and decision.deny_reason_code == "explicit_deny":
                        observed = "deny"
                finally:
                    await nested.rollback()
            elif expected == "hold":
                conflict_candidate = OntologyCandidatePatch.model_validate(
                    {
                        "schema_version": "hive.company_ontology_candidate.v1",
                        "snapshot_complete": True,
                        "coverage_ledger": {
                            "complete": True,
                            "total_units": 1,
                            "covered_units": 1,
                            "missing_units": [],
                        },
                        "conflict_ledger": {"unresolved": [{"case_ref": case.get("case_ref")}]},
                    }
                )
                try:
                    validate_ontology_candidate(conflict_candidate)
                except OntologyCandidateRejected as exc:
                    if "unresolved conflicts" in str(exc):
                        observed = "hold"
            conflict_results.append(
                {
                    "case_ref": case.get("case_ref"),
                    "passed": observed == expected,
                    "observed": observed,
                    "expected": expected,
                }
            )

        temporal_results: list[dict[str, Any]] = []
        for case in bundle.acceptance.temporal_cases:
            expected = str(case.get("expected") or "")
            observed = "unsupported"
            if expected == "not_active":
                decision = await resolve_company_knowledge_permission(
                    session,
                    principal=principal,
                    resource=CompanyKnowledgeResource(
                        tenant_id=principal.tenant_id,
                        resource_type="company_ontology_namespace",
                        resource_id=None,
                        resource_key=f"namespace:{activation.namespace}",
                        namespace=activation.namespace,
                        sensitivity="PL1_public",
                        source_acl_snapshot_hash=bundle.content_hash,
                        source_acl={"all_tenant_members": True},
                        evidence_access_complete=True,
                        publication_status="active",
                        validity_active=False,
                    ),
                    action="query",
                )
                if not decision.allowed and decision.deny_reason_code == "publication_not_active":
                    observed = "not_active"
            temporal_results.append(
                {
                    "case_ref": case.get("case_ref"),
                    "passed": observed == expected,
                    "observed": observed,
                    "expected": expected,
                }
            )

        all_acceptance_results = (
            query_results,
            action_results,
            acl_results,
            conflict_results,
            temporal_results,
        )
        receipt = {
            "schema": "hive.company_ontology_activation_dry_run.v1",
            "idempotency_key": request_key,
            "attempt": attempt,
            "passed": (
                not engine_unavailable
                and package_validation.get("passed") is True
                and all(item["passed"] for results in all_acceptance_results for item in results)
            ),
            "retryable": engine_unavailable,
            "package_hash": bundle.content_hash,
            "engine_validation": package_validation,
            "golden_actions": action_results,
            "golden_queries": query_results,
            "acl_cases": acl_results,
            "conflict_cases": conflict_results,
            "temporal_cases": temporal_results,
            "effect_committed": False,
        }
        activation.dry_run_receipt_json = receipt
        installation.acceptance_receipt_json = receipt
        if receipt["passed"] is not True:
            activation.status = "blocked"
            await append_company_knowledge_event(
                session,
                event_input=_event_input(
                    principal=principal,
                    event_type="company_ontology.activation_dry_run_failed",
                    resource_type="ontology_activation",
                    resource_id=activation.id,
                    resource_version=activation.activation_version,
                    source_refs=(f"ontology-package://{bundle.content_hash}",),
                    source_hash=bundle.content_hash,
                    policy_snapshot=policy,
                    trace_id=trace_id,
                    idempotency_key=f"{request_key}:failed",
                    outcome="blocked",
                    payload={
                        "namespace": activation.namespace,
                        "retryable": receipt["retryable"],
                        "receipt": receipt,
                    },
                ),
            )
            return activation
        current = (
            (
                await session.execute(
                    select(CompanyOntologyActivation)
                    .where(
                        CompanyOntologyActivation.tenant_id == principal.tenant_id,
                        CompanyOntologyActivation.namespace == activation.namespace,
                        CompanyOntologyActivation.status == "active",
                        CompanyOntologyActivation.id != activation.id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for previous in current:
            previous.status = "superseded"
        activation.status = "active"
        activation.activated_by_user_id = principal.accountable_user_id
        activation.activated_at = _utcnow()
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_ontology.activation_dry_run_passed",
                resource_type="ontology_activation",
                resource_id=activation.id,
                resource_version=activation.activation_version,
                source_refs=(f"ontology-package://{bundle.content_hash}",),
                source_hash=bundle.content_hash,
                policy_snapshot=policy,
                trace_id=trace_id,
                idempotency_key=f"{request_key}:passed",
                outcome="active",
                payload={"namespace": activation.namespace, "receipt": receipt},
            ),
        )
        return activation

    async def start_curation(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: OntologyCurationRequest,
    ) -> OntologyCurationResult:
        if principal.actor_type != "agent" or principal.actor_id is None:
            raise PermissionError("company_ontology_agent_runtime_curation_required")
        runtime_receipt = dict(request.model_execution_receipt or {})
        if str(runtime_receipt.get("agent_id") or "") != str(principal.actor_id):
            raise PermissionError("company_ontology_model_receipt_agent_mismatch")
        incoming_semantic_hash = ontology_semantic_candidate_hash(request.candidate_patch)
        candidate = bind_runtime_model_receipt(
            request.candidate_patch,
            runtime_receipt=runtime_receipt,
        )
        activation = (
            await session.execute(
                select(CompanyOntologyActivation)
                .where(
                    CompanyOntologyActivation.id == request.activation_id,
                    CompanyOntologyActivation.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if activation is None:
            raise LookupError("company_ontology_activation_not_found")
        if activation.status != "active" or dict(activation.dry_run_receipt_json or {}).get("passed") is not True:
            raise ValueError("company_ontology_active_dry_run_required")
        _installation, _package_version, bundle = await self._installation_bundle(
            session,
            tenant_id=principal.tenant_id,
            installation_id=activation.installation_id,
        )
        policy = await self._require_permission(
            session,
            principal=principal,
            namespace=activation.namespace,
            action="curate",
        )
        existing = (
            await session.execute(
                select(CompanyOntologyCurationRun)
                .where(
                    CompanyOntologyCurationRun.tenant_id == principal.tenant_id,
                    CompanyOntologyCurationRun.idempotency_key == request.idempotency_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        candidate_hash = ontology_candidate_hash(candidate)
        run: CompanyOntologyCurationRun
        attempt = 1
        if existing is not None:
            if (
                existing.created_by_type != "agent"
                or existing.created_by_id != principal.actor_id
                or existing.accountable_user_id != principal.accountable_user_id
            ):
                raise PermissionError("company_ontology_curation_owner_mismatch")
            stored_candidate = OntologyCandidatePatch.model_validate(existing.candidate_patch_json)
            if (
                ontology_semantic_candidate_hash(stored_candidate) != incoming_semantic_hash
                or existing.activation_id != request.activation_id
                or existing.baseline_release_id != request.baseline_release_id
                or _hash(existing.source_contract_versions_json)
                != _hash([dict(item) for item in request.source_contract_versions])
                or _hash(existing.evidence_scope_json) != _hash(dict(request.evidence_scope))
                or list(existing.requested_operations_json or []) != list(request.requested_operations)
            ):
                raise ValueError("company_ontology_curation_idempotency_conflict")
            proposal = await self._proposal_for_run(
                session,
                tenant_id=principal.tenant_id,
                run_id=existing.id,
            )
            if existing.status != "held" or dict(existing.retry_state_json or {}).get("retryable") is not True:
                return OntologyCurationResult(run=existing, proposal=proposal)
            if proposal is not None:
                raise ValueError("company_ontology_retryable_run_has_proposal")
            candidate = validate_ontology_candidate(stored_candidate)
            candidate_hash = ontology_candidate_hash(candidate)
            if existing.candidate_patch_hash != candidate_hash:
                raise ValueError("company_ontology_curation_stored_candidate_hash_mismatch")
            attempt = max(1, int(dict(existing.retry_state_json or {}).get("attempt") or 1)) + 1
            run = existing
            run.status = "running"
            run.retry_state_json = {"attempt": attempt, "retryable": False}
            run.error_code = None
            run.acceptance_result_json = {
                "schema": "hive.company_ontology_curation_acceptance.v1",
                "passed": False,
                "recovery_in_progress": True,
                "candidate_preserved": True,
                "attempt": attempt,
            }
            await append_company_knowledge_event(
                session,
                event_input=_event_input(
                    principal=principal,
                    event_type="company_ontology.curation_retried",
                    resource_type="ontology_curation_run",
                    resource_id=run.id,
                    resource_version=None,
                    source_refs=tuple(self._candidate_source_refs(candidate)),
                    source_hash=candidate_hash,
                    policy_snapshot=policy,
                    trace_id=request.trace_id,
                    idempotency_key=f"{request.idempotency_key}:retried:{attempt}",
                    outcome="running",
                    payload={
                        "attempt": attempt,
                        "checkpoint_ref": run.checkpoint_ref,
                        "candidate_preserved": True,
                    },
                ),
            )
        else:
            runtime_task_id = candidate.model_prompt_receipts[0].runtime_task_id
            if runtime_task_id is not None:
                runtime_task = (
                    await session.execute(
                        select(RuntimeTask).where(
                            RuntimeTask.id == runtime_task_id,
                            RuntimeTask.tenant_id == principal.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if (
                    runtime_task is None
                    or (
                        runtime_task.parent_agent_id != principal.actor_id
                        and runtime_task.child_agent_id != principal.actor_id
                    )
                    or (
                        runtime_task.root_user_id is not None
                        and runtime_task.root_user_id != principal.accountable_user_id
                    )
                ):
                    raise PermissionError("company_ontology_model_receipt_task_mismatch")
            run = CompanyOntologyCurationRun(
                tenant_id=principal.tenant_id,
                runtime_task_id=runtime_task_id,
                activation_id=activation.id,
                baseline_release_id=request.baseline_release_id,
                idempotency_key=request.idempotency_key,
                source_contract_versions_json=[dict(item) for item in request.source_contract_versions],
                evidence_scope_json=dict(request.evidence_scope),
                authority_snapshot_json={
                    "principal": principal.evidence(),
                    "permission_decision": policy,
                    "model_execution": candidate.model_prompt_receipts[0].model_dump(mode="json"),
                    "captured_at": _utcnow().isoformat(),
                },
                requested_operations_json=list(request.requested_operations),
                model_prompt_receipts_json=[item.model_dump(mode="json") for item in candidate.model_prompt_receipts],
                candidate_patch_ref=None,
                candidate_patch_hash=candidate_hash,
                candidate_patch_json=candidate.model_dump(mode="json"),
                coverage_ledger_json=candidate.coverage_ledger.model_dump(mode="json"),
                conflict_ledger_json=candidate.conflict_ledger.model_dump(mode="json"),
                unresolved_questions_json=list(candidate.unresolved_questions),
                acceptance_result_json={
                    "schema": "hive.company_ontology_curation_acceptance.v1",
                    "passed": False,
                    "validation_in_progress": True,
                    "candidate_preserved": True,
                    "attempt": attempt,
                },
                status="running",
                retry_state_json={"attempt": attempt, "retryable": False},
                checkpoint_ref=None,
                error_code=None,
                created_by_type=principal.actor_type,
                created_by_id=principal.actor_id,
                accountable_user_id=principal.accountable_user_id,
            )
            session.add(run)
            await session.flush()
            run.candidate_patch_ref = f"company-ontology-candidate://{run.id}"
            run.checkpoint_ref = f"company-ontology-checkpoint://{run.id}/candidate-received"
            await append_company_knowledge_event(
                session,
                event_input=_event_input(
                    principal=principal,
                    event_type="company_ontology.curation_started",
                    resource_type="ontology_curation_run",
                    resource_id=run.id,
                    resource_version=None,
                    source_refs=tuple(self._candidate_source_refs(candidate)),
                    source_hash=candidate_hash,
                    policy_snapshot=policy,
                    trace_id=request.trace_id,
                    idempotency_key=f"{request.idempotency_key}:started",
                    outcome="running",
                    payload={
                        "attempt": attempt,
                        "candidate_ref": run.candidate_patch_ref,
                        "checkpoint_ref": run.checkpoint_ref,
                    },
                ),
            )

        try:
            candidate = validate_ontology_candidate(candidate)
            if ontology_candidate_hash(candidate) != candidate_hash:
                raise OntologyCandidateRejected("candidate_hash_mismatch")
            await self._validate_source_contract_versions(
                session,
                tenant_id=principal.tenant_id,
                versions=request.source_contract_versions,
            )
            evidence_receipts = await self._validate_candidate_evidence(
                session,
                principal=principal,
                namespace=activation.namespace,
                candidate=candidate,
            )
            run.checkpoint_ref = f"company-ontology-checkpoint://{run.id}/candidate-validated"
        except (OntologyCandidateRejected, ValueError, PermissionError) as exc:
            run.status = "held"
            run.retry_state_json = {"attempt": attempt, "retryable": False}
            run.checkpoint_ref = f"company-ontology-checkpoint://{run.id}/candidate-preserved"
            run.error_code = str(exc)[:120]
            run.acceptance_result_json = {
                "schema": "hive.company_ontology_curation_acceptance.v1",
                "passed": False,
                "hold_reason": str(exc),
                "candidate_preserved": True,
                "retryable": False,
                "attempt": attempt,
            }
            await append_company_knowledge_event(
                session,
                event_input=_event_input(
                    principal=principal,
                    event_type="company_ontology.curation_held",
                    resource_type="ontology_curation_run",
                    resource_id=run.id,
                    resource_version=None,
                    source_refs=tuple(self._candidate_source_refs(candidate)),
                    source_hash=candidate_hash,
                    policy_snapshot=policy,
                    trace_id=request.trace_id,
                    idempotency_key=f"{request.idempotency_key}:held:{attempt}",
                    outcome="held",
                    payload={
                        "reason": str(exc),
                        "candidate_preserved": True,
                        "retryable": False,
                        "attempt": attempt,
                    },
                ),
            )
            return OntologyCurationResult(run=run, proposal=None)

        try:
            validation = await self._engine.validate_candidate(
                package=bundle,
                candidate=candidate,
            )
            if validation.get("passed") is not True:
                raise OntologyCandidateRejected("candidate_engine_validation_failed")
        except (OntologyCandidateRejected, ValueError, PermissionError) as exc:
            run.status = "held"
            run.retry_state_json = {"attempt": attempt, "retryable": False}
            run.checkpoint_ref = f"company-ontology-checkpoint://{run.id}/candidate-preserved"
            run.error_code = str(exc)[:120]
            run.acceptance_result_json = {
                "schema": "hive.company_ontology_curation_acceptance.v1",
                "passed": False,
                "hold_reason": str(exc),
                "candidate_preserved": True,
                "retryable": False,
                "attempt": attempt,
            }
            await append_company_knowledge_event(
                session,
                event_input=_event_input(
                    principal=principal,
                    event_type="company_ontology.curation_held",
                    resource_type="ontology_curation_run",
                    resource_id=run.id,
                    resource_version=None,
                    source_refs=tuple(self._candidate_source_refs(candidate)),
                    source_hash=candidate_hash,
                    policy_snapshot=policy,
                    trace_id=request.trace_id,
                    idempotency_key=f"{request.idempotency_key}:held:{attempt}",
                    outcome="held",
                    payload={
                        "reason": str(exc),
                        "candidate_preserved": True,
                        "retryable": False,
                        "attempt": attempt,
                    },
                ),
            )
            return OntologyCurationResult(run=run, proposal=None)
        except Exception:  # noqa: BLE001 - provider outage is a typed, retryable hold
            run.status = "held"
            run.retry_state_json = {"attempt": attempt, "retryable": True}
            run.checkpoint_ref = f"company-ontology-checkpoint://{run.id}/candidate-preserved"
            run.error_code = "company_ontology_engine_unavailable"
            run.acceptance_result_json = {
                "schema": "hive.company_ontology_curation_acceptance.v1",
                "passed": False,
                "hold_reason": "company_ontology_engine_unavailable",
                "candidate_preserved": True,
                "retryable": True,
                "attempt": attempt,
            }
            await append_company_knowledge_event(
                session,
                event_input=_event_input(
                    principal=principal,
                    event_type="company_ontology.curation_held",
                    resource_type="ontology_curation_run",
                    resource_id=run.id,
                    resource_version=None,
                    source_refs=tuple(self._candidate_source_refs(candidate)),
                    source_hash=candidate_hash,
                    policy_snapshot=policy,
                    trace_id=request.trace_id,
                    idempotency_key=f"{request.idempotency_key}:held:{attempt}",
                    outcome="held",
                    payload={
                        "reason": "company_ontology_engine_unavailable",
                        "candidate_preserved": True,
                        "retryable": True,
                        "attempt": attempt,
                    },
                ),
            )
            return OntologyCurationResult(run=run, proposal=None)

        run.acceptance_result_json = {
            "schema": "hive.company_ontology_curation_acceptance.v1",
            "passed": True,
            "package_hash": bundle.content_hash,
            "deterministic_validation": validation,
            "evidence_receipts": evidence_receipts,
            "model_receipts_complete": bool(candidate.model_prompt_receipts),
            "candidate_preserved": True,
            "attempt": attempt,
        }
        proposal = await self._knowledge_service.create_proposal(
            session,
            principal=principal,
            request=CompanyKnowledgeProposalRequest(
                proposal_kind="ontology",
                source_id=None,
                source_document_id=None,
                source_revision_ref=run.candidate_patch_ref,
                baseline_publication_id=None,
                baseline_version=None,
                proposed_patch=candidate.model_dump(mode="json"),
                proposed_namespace=activation.namespace,
                proposed_sensitivity=self._candidate_sensitivity(candidate),
                source_refs=tuple(self._candidate_source_refs(candidate)),
                source_coverage=candidate.coverage_ledger.model_dump(mode="json"),
                conflict_candidates=tuple(candidate.conflict_ledger.unresolved),
                ontology_mapping={
                    "curation_run_id": str(run.id),
                    "activation_id": str(activation.id),
                    "package_hash": bundle.content_hash,
                },
                risk_level="high",
                required_review_policy={
                    "minimum_approvals": 1,
                    "required_roles": ["org_admin"],
                    "separation": principal.actor_type == "agent",
                },
                idempotency_key=f"{request.idempotency_key}:proposal",
                trace_id=request.trace_id,
            ),
        )
        proposal = await self._knowledge_service.submit_proposal(
            session,
            principal=principal,
            proposal_id=proposal.id,
            expected_state_version=proposal.state_version,
            trace_id=request.trace_id,
        )
        run.status = "completed"
        run.retry_state_json = {"attempt": attempt, "retryable": False}
        run.checkpoint_ref = f"company-ontology-checkpoint://{run.id}/proposal-submitted"
        run.error_code = None
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_ontology.curation_completed",
                resource_type="ontology_curation_run",
                resource_id=run.id,
                resource_version=None,
                source_refs=tuple(self._candidate_source_refs(candidate)),
                source_hash=run.candidate_patch_hash,
                policy_snapshot=policy,
                trace_id=request.trace_id,
                idempotency_key=f"{request.idempotency_key}:completed",
                outcome="completed",
                payload={
                    "proposal_id": str(proposal.id),
                    "proposal_status": proposal.status,
                    "attempt": attempt,
                    "checkpoint_ref": run.checkpoint_ref,
                },
            ),
        )
        return OntologyCurationResult(run=run, proposal=proposal)

    async def publish_curation_run(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        run_id: uuid.UUID,
        valid_from: datetime,
        valid_until: datetime | None,
        trace_id: str,
    ) -> CompanyOntologyRelease:
        run = (
            await session.execute(
                select(CompanyOntologyCurationRun)
                .where(
                    CompanyOntologyCurationRun.id == run_id,
                    CompanyOntologyCurationRun.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            raise LookupError("company_ontology_curation_run_not_found")
        if run.status != "completed" or dict(run.acceptance_result_json or {}).get("passed") is not True:
            raise ValueError("company_ontology_completed_curation_required")
        activation = (
            await session.execute(
                select(CompanyOntologyActivation)
                .where(
                    CompanyOntologyActivation.id == run.activation_id,
                    CompanyOntologyActivation.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        _installation, package_version, bundle = await self._installation_bundle(
            session,
            tenant_id=principal.tenant_id,
            installation_id=activation.installation_id,
        )
        proposal = await self._proposal_for_run(
            session,
            tenant_id=principal.tenant_id,
            run_id=run.id,
            for_update=True,
        )
        if proposal is not None and proposal.status == "published":
            existing_release = (
                await session.execute(
                    select(CompanyOntologyRelease).where(
                        CompanyOntologyRelease.tenant_id == principal.tenant_id,
                        CompanyOntologyRelease.curation_run_id == run.id,
                        CompanyOntologyRelease.proposal_id == proposal.id,
                    )
                )
            ).scalar_one_or_none()
            if existing_release is None:
                raise ValueError("company_ontology_published_proposal_release_missing")
            if existing_release.valid_from != valid_from or existing_release.valid_until != valid_until:
                raise ValueError("company_ontology_publish_idempotency_conflict")
            return existing_release
        if proposal is None or proposal.status != "approved":
            raise ValueError("company_ontology_approved_proposal_required")
        candidate = validate_ontology_candidate(OntologyCandidatePatch.model_validate(run.candidate_patch_json))
        if proposal.proposed_content_hash != ontology_candidate_hash(candidate):
            raise ValueError("company_ontology_proposal_candidate_hash_mismatch")
        review_evaluation, review_receipts = await self._review_receipts(
            session,
            proposal=proposal,
        )
        if review_evaluation["approved"] is not True:
            raise ValueError("company_ontology_complete_review_set_required")
        policy = await self._require_permission(
            session,
            principal=principal,
            namespace=activation.namespace,
            action="publish",
            resource_type="company_knowledge_proposal",
            resource_id=proposal.id,
            resource_key=f"proposal:{proposal.id}",
            sensitivity=proposal.proposed_sensitivity,
        )
        await self._validate_release_gate(
            session,
            principal=principal,
            activation=activation,
            bundle=bundle,
            run=run,
            candidate=candidate,
        )
        return await self._materialize_release(
            session,
            principal=principal,
            activation=activation,
            package_version=package_version,
            bundle=bundle,
            run=run,
            proposal=proposal,
            candidate=candidate,
            review_evaluation=review_evaluation,
            review_receipts=review_receipts,
            valid_from=valid_from,
            valid_until=valid_until,
            trace_id=trace_id,
            policy=policy,
            restored_from_release_id=None,
        )

    async def retire_release(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        release_id: uuid.UUID,
        request: OntologyReleaseLifecycleRequest,
    ) -> CompanyOntologyRelease:
        release = await self._locked_release(
            session,
            tenant_id=principal.tenant_id,
            release_id=release_id,
        )
        if release.status == "retired":
            return release
        if release.status != "active":
            raise ValueError("company_ontology_active_release_required")
        policy = await self._require_permission(
            session,
            principal=principal,
            namespace=release.namespace,
            action="retire",
            resource_type="company_ontology_release",
            resource_id=release.id,
            resource_key=f"ontology-release:{release.id}",
        )
        now = _utcnow()
        release.status = "retired"
        release.retired_at = now
        release.valid_until = release.valid_until or now
        await append_company_knowledge_event_with_outbox(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_ontology.release_retired",
                resource_type="ontology_release",
                resource_id=release.id,
                resource_version=release.version,
                source_refs=(f"company-ontology-release://{release.id}",),
                source_hash=release.release_hash,
                policy_snapshot=policy,
                trace_id=request.trace_id,
                idempotency_key=f"ontology-release:{release.id}:retired",
                outcome="retired",
                payload={"reason": request.reason},
            ),
            aggregate_type="ontology_release",
            aggregate_id=release.id,
            outbox_event_type="company_ontology.projection_tombstone_requested",
            outbox_idempotency_key=f"ontology-release:{release.id}:tombstone",
            outbox_payload={
                "operation": "tombstone_ontology_release",
                "release_id": str(release.id),
                "release_hash": release.release_hash,
            },
            available_at=now,
        )
        return release

    async def restore_release(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        release_id: uuid.UUID,
        request: OntologyReleaseLifecycleRequest,
    ) -> CompanyOntologyRelease:
        source_release = await self._locked_release(
            session,
            tenant_id=principal.tenant_id,
            release_id=release_id,
        )
        if source_release.status not in {"retired", "superseded"}:
            raise ValueError("company_ontology_retired_or_superseded_release_required")
        if request.approved_proposal_id is None or request.valid_from is None:
            raise ValueError("company_ontology_restore_requires_approved_proposal_and_valid_from")
        proposal = (
            await session.execute(
                select(CompanyKnowledgeProposal)
                .where(
                    CompanyKnowledgeProposal.id == request.approved_proposal_id,
                    CompanyKnowledgeProposal.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if proposal is not None and proposal.status == "published" and proposal.proposal_kind == "ontology":
            existing_restore = (
                await session.execute(
                    select(CompanyOntologyRelease).where(
                        CompanyOntologyRelease.tenant_id == principal.tenant_id,
                        CompanyOntologyRelease.proposal_id == proposal.id,
                        CompanyOntologyRelease.restored_from_release_id == source_release.id,
                    )
                )
            ).scalar_one_or_none()
            if existing_restore is None:
                raise ValueError("company_ontology_published_restore_release_missing")
            if existing_restore.valid_from != request.valid_from:
                raise ValueError("company_ontology_restore_idempotency_conflict")
            return existing_restore
        if proposal is None or proposal.status != "approved" or proposal.proposal_kind != "ontology":
            raise ValueError("company_ontology_approved_restore_proposal_required")
        patch = dict(proposal.proposed_patch_json or {})
        if patch.get("operation") != "restore_ontology_release" or str(patch.get("release_id")) != str(
            source_release.id
        ):
            raise ValueError("company_ontology_restore_proposal_mismatch")
        run = (
            await session.execute(
                select(CompanyOntologyCurationRun).where(
                    CompanyOntologyCurationRun.id == source_release.curation_run_id,
                    CompanyOntologyCurationRun.tenant_id == principal.tenant_id,
                )
            )
        ).scalar_one()
        activation = (
            await session.execute(
                select(CompanyOntologyActivation).where(
                    CompanyOntologyActivation.id == source_release.activation_id,
                    CompanyOntologyActivation.tenant_id == principal.tenant_id,
                )
            )
        ).scalar_one()
        _installation, package_version, bundle = await self._installation_bundle(
            session,
            tenant_id=principal.tenant_id,
            installation_id=activation.installation_id,
        )
        candidate = validate_ontology_candidate(OntologyCandidatePatch.model_validate(run.candidate_patch_json))
        review_evaluation, review_receipts = await self._review_receipts(
            session,
            proposal=proposal,
        )
        if review_evaluation["approved"] is not True:
            raise ValueError("company_ontology_complete_review_set_required")
        policy = await self._require_permission(
            session,
            principal=principal,
            namespace=source_release.namespace,
            action="restore",
            resource_type="company_ontology_release",
            resource_id=source_release.id,
            resource_key=f"ontology-release:{source_release.id}",
        )
        await self._validate_release_gate(
            session,
            principal=principal,
            activation=activation,
            bundle=bundle,
            run=run,
            candidate=candidate,
        )
        return await self._materialize_release(
            session,
            principal=principal,
            activation=activation,
            package_version=package_version,
            bundle=bundle,
            run=run,
            proposal=proposal,
            candidate=candidate,
            review_evaluation=review_evaluation,
            review_receipts=review_receipts,
            valid_from=request.valid_from,
            valid_until=None,
            trace_id=request.trace_id,
            policy=policy,
            restored_from_release_id=source_release.id,
        )

    async def _installation_bundle(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        installation_id: uuid.UUID,
        for_update: bool = False,
    ) -> tuple[
        CompanyOntologyPackageInstallation,
        CompanyOntologyPackageVersion,
        OntologyPackageBundle,
    ]:
        statement = (
            select(
                CompanyOntologyPackageInstallation,
                CompanyOntologyPackageVersion,
                CompanyOntologyPackage,
            )
            .join(
                CompanyOntologyPackageVersion,
                CompanyOntologyPackageVersion.id == CompanyOntologyPackageInstallation.package_version_id,
            )
            .join(
                CompanyOntologyPackage,
                CompanyOntologyPackage.id == CompanyOntologyPackageVersion.package_id,
            )
            .where(
                CompanyOntologyPackageInstallation.id == installation_id,
                CompanyOntologyPackageInstallation.tenant_id == tenant_id,
                CompanyOntologyPackageVersion.tenant_id == tenant_id,
                CompanyOntologyPackage.tenant_id == tenant_id,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            raise LookupError("company_ontology_installation_not_found")
        installation, package_version, package = row
        bundle = load_builtin_ontology_catalog().get(
            package.package_key,
            package_version.version,
        )
        if bundle is None or bundle.content_hash != package_version.content_hash:
            raise ValueError("company_ontology_installed_package_source_unavailable")
        return installation, package_version, bundle

    @staticmethod
    async def _validate_installation_graph(
        session: Any,
        *,
        tenant_id: uuid.UUID,
        bundle: OntologyPackageBundle,
    ) -> None:
        rows = (
            await session.execute(
                select(
                    CompanyOntologyPackage.package_key,
                    CompanyOntologyPackageVersion.version,
                )
                .join(
                    CompanyOntologyPackageVersion,
                    CompanyOntologyPackageVersion.package_id == CompanyOntologyPackage.id,
                )
                .join(
                    CompanyOntologyPackageInstallation,
                    CompanyOntologyPackageInstallation.package_version_id == CompanyOntologyPackageVersion.id,
                )
                .where(
                    CompanyOntologyPackage.tenant_id == tenant_id,
                    CompanyOntologyPackageVersion.tenant_id == tenant_id,
                    CompanyOntologyPackageInstallation.tenant_id == tenant_id,
                    CompanyOntologyPackageInstallation.status == "installed",
                )
            )
        ).all()
        installed = {(str(package_key), str(version)) for package_key, version in rows}
        missing_dependencies = sorted(
            {
                (item.package_key, item.version)
                for item in bundle.manifest.dependencies
                if (item.package_key, item.version) not in installed
            }
        )
        if missing_dependencies:
            raise ValueError(
                "company_ontology_package_dependency_missing:"
                + ",".join(f"{package_key}@{version}" for package_key, version in missing_dependencies)
            )
        installed_conflicts = sorted(
            {
                (item.package_key, item.version)
                for item in bundle.manifest.conflicts
                if (item.package_key, item.version) in installed
            }
        )
        if installed_conflicts:
            raise ValueError(
                "company_ontology_package_conflict:"
                + ",".join(f"{package_key}@{version}" for package_key, version in installed_conflicts)
            )

    @staticmethod
    async def _validate_source_contract_versions(
        session: Any,
        *,
        tenant_id: uuid.UUID,
        versions: tuple[dict[str, Any], ...],
    ) -> None:
        if not versions:
            raise ValueError("company_ontology_source_contract_versions_required")
        for item in versions:
            contract_id = uuid.UUID(str(item.get("source_contract_id")))
            version = int(item.get("version") or 0)
            contract = (
                await session.execute(
                    select(CompanyKnowledgeSourceContract).where(
                        CompanyKnowledgeSourceContract.id == contract_id,
                        CompanyKnowledgeSourceContract.tenant_id == tenant_id,
                        CompanyKnowledgeSourceContract.version == version,
                        CompanyKnowledgeSourceContract.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if contract is None:
                raise ValueError("company_ontology_source_contract_version_unavailable")

    async def _validate_candidate_evidence(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        namespace: str,
        candidate: OntologyCandidatePatch,
    ) -> list[dict[str, Any]]:
        sensitivity_by_ref: dict[str, str] = {}
        for item in (
            *candidate.objects,
            *candidate.assertions,
            *candidate.links,
            *candidate.events,
            *candidate.definition_overrides.object_types,
            *candidate.definition_overrides.property_types,
            *candidate.definition_overrides.link_types,
            *candidate.definition_overrides.event_types,
        ):
            sensitivity = canonicalize_sensitivity(item.sensitivity).value
            for source_ref in item.source_refs:
                current = sensitivity_by_ref.get(source_ref)
                if current is None or sensitivity_rank(sensitivity) > sensitivity_rank(current):
                    sensitivity_by_ref[source_ref] = sensitivity
        receipts: list[dict[str, Any]] = []
        for source_ref in sorted(sensitivity_by_ref):
            evidence_id = _evidence_id(source_ref)
            evidence = (
                await session.execute(
                    select(CompanyKnowledgeEvidence).where(
                        CompanyKnowledgeEvidence.id == evidence_id,
                        CompanyKnowledgeEvidence.tenant_id == principal.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if evidence is None or evidence.status != "accepted":
                raise ValueError("company_ontology_evidence_unavailable")
            policy = await self._require_permission(
                session,
                principal=principal,
                namespace=namespace,
                action="read",
                resource_type="company_knowledge_evidence",
                resource_id=evidence.id,
                resource_key=f"evidence:{evidence.id}",
                sensitivity=sensitivity_by_ref[source_ref],
                source_acl_snapshot_hash=evidence.source_acl_snapshot_hash,
                source_acl=dict(evidence.source_acl_snapshot_json or {}),
                evidence_access_complete=bool(dict(evidence.coverage_ledger_json or {}).get("complete")),
                publication_status="active",
            )
            receipts.append(
                {
                    "evidence_id": str(evidence.id),
                    "content_hash": evidence.content_hash,
                    "source_acl_snapshot_hash": evidence.source_acl_snapshot_hash,
                    "permission": policy,
                }
            )
        if not receipts:
            raise ValueError("company_ontology_evidence_required")
        return receipts

    async def _validate_release_gate(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        activation: CompanyOntologyActivation,
        bundle: OntologyPackageBundle,
        run: CompanyOntologyCurationRun,
        candidate: OntologyCandidatePatch,
    ) -> None:
        if activation.status != "active":
            raise ValueError("company_ontology_active_activation_required")
        dry_run = dict(activation.dry_run_receipt_json or {})
        if dry_run.get("passed") is not True or dry_run.get("package_hash") != bundle.content_hash:
            raise ValueError("company_ontology_current_dry_run_required")
        if run.candidate_patch_hash != ontology_candidate_hash(candidate):
            raise ValueError("company_ontology_candidate_hash_mismatch")
        try:
            validation = await self._engine.validate_candidate(
                package=bundle,
                candidate=candidate,
            )
        except Exception as exc:
            raise OntologyEngineUnavailable("company_ontology_engine_unavailable") from exc
        if validation.get("passed") is not True:
            raise ValueError("company_ontology_release_validation_failed")
        await self._validate_candidate_evidence(
            session,
            principal=principal,
            namespace=activation.namespace,
            candidate=candidate,
        )

    async def _materialize_release(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        activation: CompanyOntologyActivation,
        package_version: CompanyOntologyPackageVersion,
        bundle: OntologyPackageBundle,
        run: CompanyOntologyCurationRun,
        proposal: CompanyKnowledgeProposal,
        candidate: OntologyCandidatePatch,
        review_evaluation: dict[str, Any],
        review_receipts: list[dict[str, Any]],
        valid_from: datetime,
        valid_until: datetime | None,
        trace_id: str,
        policy: dict[str, Any],
        restored_from_release_id: uuid.UUID | None,
    ) -> CompanyOntologyRelease:
        if valid_until is not None and valid_until <= valid_from:
            raise ValueError("company_ontology_release_validity_invalid")
        active_releases = (
            (
                await session.execute(
                    select(CompanyOntologyRelease)
                    .where(
                        CompanyOntologyRelease.tenant_id == principal.tenant_id,
                        CompanyOntologyRelease.namespace == activation.namespace,
                        CompanyOntologyRelease.status == "active",
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        version = (
            int(
                await session.scalar(
                    select(func.coalesce(func.max(CompanyOntologyRelease.version), 0)).where(
                        CompanyOntologyRelease.tenant_id == principal.tenant_id,
                        CompanyOntologyRelease.namespace == activation.namespace,
                    )
                )
                or 0
            )
            + 1
        )
        release_hash = _hash(
            {
                "namespace": activation.namespace,
                "version": version,
                "package_hash": bundle.content_hash,
                "candidate_hash": run.candidate_patch_hash,
                "review_set_hash": review_evaluation["review_set_hash"],
                "restored_from_release_id": restored_from_release_id,
            }
        )
        now = _utcnow()
        for previous in active_releases:
            previous.status = "superseded"
            previous.valid_until = previous.valid_until or valid_from
        try:
            validation = await self._engine.validate_candidate(
                package=bundle,
                candidate=candidate,
            )
        except Exception as exc:
            raise OntologyEngineUnavailable("company_ontology_engine_unavailable") from exc
        release = CompanyOntologyRelease(
            tenant_id=principal.tenant_id,
            namespace=activation.namespace,
            version=version,
            activation_id=activation.id,
            package_version_id=package_version.id,
            curation_run_id=run.id,
            proposal_id=proposal.id,
            release_hash=release_hash,
            review_set_hash=review_evaluation["review_set_hash"],
            source_coverage_json=candidate.coverage_ledger.model_dump(mode="json"),
            conflict_ledger_json=candidate.conflict_ledger.model_dump(mode="json"),
            unresolved_questions_json=list(candidate.unresolved_questions),
            deterministic_validation_json=validation,
            semantic_review_receipts_json=review_receipts,
            acceptance_result_json=dict(run.acceptance_result_json or {}),
            migration_plan_json=bundle.migrations.model_dump(mode="json"),
            rollback_ref=(
                f"company-ontology-release://{restored_from_release_id}"
                if restored_from_release_id
                else (
                    f"company-ontology-release://{active_releases[0].id}"
                    if active_releases
                    else f"company-ontology-release://{release_hash}/none"
                )
            ),
            projection_rebuild_plan_json={
                "provider": str(validation.get("provider") or "replaceable_engine"),
                "operation": "rebuild_projection",
                "source": "immutable_release_membership",
            },
            status="active",
            supersedes_release_id=active_releases[0].id if active_releases else None,
            restored_from_release_id=restored_from_release_id,
            published_by_user_id=principal.accountable_user_id,
            published_at=now,
            valid_from=valid_from,
            valid_until=valid_until,
            retired_at=None,
        )
        session.add(release)
        await session.flush()
        await self._materialize_definitions(
            session,
            tenant_id=principal.tenant_id,
            namespace=activation.namespace,
            release=release,
            bundle=bundle,
            candidate=candidate,
        )
        await self._materialize_candidate_items(
            session,
            tenant_id=principal.tenant_id,
            namespace=activation.namespace,
            release=release,
            run=run,
            candidate=candidate,
        )
        try:
            projection = await self._engine.materialize_release_projection(
                {
                    "release_id": release.id,
                    "release_hash": release.release_hash,
                    "namespace": release.namespace,
                }
            )
        except Exception as exc:
            raise OntologyEngineUnavailable("company_ontology_engine_unavailable") from exc
        release.projection_rebuild_plan_json = {
            **dict(release.projection_rebuild_plan_json or {}),
            "materialization_receipt": projection,
        }
        proposal.status = next_company_proposal_status(proposal.status, "begin_publish")
        proposal.status = next_company_proposal_status(proposal.status, "publish_succeeded")
        proposal.state_version += 2
        await append_company_knowledge_event_with_outbox(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_ontology.release_published",
                resource_type="ontology_release",
                resource_id=release.id,
                resource_version=release.version,
                source_refs=tuple(self._candidate_source_refs(candidate)),
                source_hash=release.release_hash,
                policy_snapshot=policy,
                trace_id=trace_id,
                idempotency_key=f"ontology-release:{release.id}:published",
                outcome="active",
                payload={
                    "namespace": release.namespace,
                    "version": release.version,
                    "proposal_id": str(proposal.id),
                    "restored_from_release_id": (str(restored_from_release_id) if restored_from_release_id else None),
                },
            ),
            aggregate_type="ontology_release",
            aggregate_id=release.id,
            outbox_event_type="company_ontology.projection_requested",
            outbox_idempotency_key=f"ontology-release:{release.id}:project",
            outbox_payload={
                "operation": "project_ontology_release",
                "release_id": str(release.id),
                "release_hash": release.release_hash,
            },
            available_at=now,
        )
        return release

    async def _materialize_definitions(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        namespace: str,
        release: CompanyOntologyRelease,
        bundle: OntologyPackageBundle,
        candidate: OntologyCandidatePatch,
    ) -> None:
        object_types = {
            item.type_ref: item for item in (*bundle.schema.object_types, *candidate.definition_overrides.object_types)
        }
        property_types = {
            item.property_ref: item
            for item in (
                *bundle.schema.property_types,
                *candidate.definition_overrides.property_types,
            )
        }
        link_types = {
            item.link_type_ref: item for item in (*bundle.schema.link_types, *candidate.definition_overrides.link_types)
        }
        event_types = {
            item.event_type_ref: item
            for item in (*bundle.schema.event_types, *candidate.definition_overrides.event_types)
        }
        rows: list[tuple[str, Any, list[str]]] = []
        for item in object_types.values():
            row = CompanyOntologyObjectType(
                tenant_id=tenant_id,
                release_id=release.id,
                namespace=namespace,
                type_ref=item.type_ref,
                schema_json=item.model_dump(mode="json"),
                source_refs_json=list(item.source_refs),
                sensitivity=item.sensitivity,
                permission_resource_ref=item.permission_resource_ref,
                status="active",
            )
            session.add(row)
            rows.append(("object_type", row, list(item.source_refs)))
        for item in property_types.values():
            row = CompanyOntologyPropertyType(
                tenant_id=tenant_id,
                release_id=release.id,
                namespace=namespace,
                property_ref=item.property_ref,
                owner_type_ref=item.object_type_refs[0],
                value_schema_json={
                    "value_type": item.value_type,
                    "object_type_refs": list(item.object_type_refs),
                },
                cardinality_json={
                    "cardinality": item.cardinality,
                    "required": item.required,
                },
                source_refs_json=list(item.source_refs),
                sensitivity=item.sensitivity,
                permission_resource_ref=item.permission_resource_ref,
                status="active",
            )
            session.add(row)
            rows.append(("property_type", row, list(item.source_refs)))
        for item in link_types.values():
            row = CompanyOntologyLinkType(
                tenant_id=tenant_id,
                release_id=release.id,
                namespace=namespace,
                link_type_ref=item.link_type_ref,
                from_type_refs_json=list(item.from_type_refs),
                to_type_refs_json=list(item.to_type_refs),
                schema_json=dict(item.properties_schema),
                source_refs_json=list(item.source_refs),
                sensitivity=item.sensitivity,
                permission_resource_ref=item.permission_resource_ref,
                status="active",
            )
            session.add(row)
            rows.append(("link_type", row, list(item.source_refs)))
        for item in event_types.values():
            row = CompanyOntologyEventType(
                tenant_id=tenant_id,
                release_id=release.id,
                namespace=namespace,
                event_type_ref=item.event_type_ref,
                schema_json=item.model_dump(mode="json"),
                source_refs_json=list(item.source_refs),
                sensitivity=item.sensitivity,
                permission_resource_ref=item.permission_resource_ref,
                status="active",
            )
            session.add(row)
            rows.append(("event_type", row, list(item.source_refs)))
        for item in bundle.rules:
            row = CompanyOntologyRuleDefinition(
                tenant_id=tenant_id,
                release_id=release.id,
                namespace=namespace,
                rule_ref=item.rule_ref,
                rule_kind=item.rule_kind,
                owner_principal_ref=item.owner_principal_ref,
                version=item.version,
                scope_json=dict(item.scope),
                input_schema_json=dict(item.input_schema),
                output_schema_json=dict(item.output_schema),
                valid_from=item.valid_from,
                valid_until=item.valid_until,
                source_refs_json=list(item.source_refs),
                examples_json=list(item.examples),
                counterexamples_json=list(item.counterexamples),
                risk=item.risk,
                review_policy_json=dict(item.review_policy),
                conflict_precedence_json=dict(item.conflict_precedence),
                evaluation_mode=item.evaluation_mode,
                acceptance_refs_json=list(item.acceptance_refs),
                sensitivity=item.sensitivity,
                permission_resource_ref=item.permission_resource_ref,
                status="active",
            )
            session.add(row)
            rows.append(("rule", row, list(item.source_refs)))
        for item in bundle.actions:
            row = CompanyOntologyActionType(
                tenant_id=tenant_id,
                release_id=release.id,
                namespace=namespace,
                action_type_ref=item.action_type_ref,
                input_schema_json=dict(item.input_schema),
                output_schema_json=dict(item.output_schema),
                required_capability=item.required_capability,
                tool_workflow_mapping_json=dict(item.tool_workflow_mapping),
                approval_policy_json=dict(item.approval_policy),
                side_effect_classification=item.side_effect_classification,
                simulation_contract_json=dict(item.simulation_contract),
                source_refs_json=list(item.source_refs),
                sensitivity=item.sensitivity,
                permission_resource_ref=item.permission_resource_ref,
                status="active",
            )
            session.add(row)
            rows.append(("action_type", row, list(item.source_refs)))
        await session.flush()
        for kind, row, source_refs in rows:
            session.add(
                CompanyOntologyReleaseItem(
                    tenant_id=tenant_id,
                    release_id=release.id,
                    item_kind=kind,
                    item_id=row.id,
                    item_hash=_hash(
                        {
                            column.name: getattr(row, column.name)
                            for column in row.__table__.columns
                            if column.name not in {"created_at", "updated_at"}
                        }
                    ),
                    source_refs_json=source_refs,
                )
            )
        await session.flush()

    async def _materialize_candidate_items(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        namespace: str,
        release: CompanyOntologyRelease,
        run: CompanyOntologyCurationRun,
        candidate: OntologyCandidatePatch,
    ) -> None:
        baseline_objects: dict[str, uuid.UUID] = {}
        if release.supersedes_release_id is not None:
            previous = (
                (
                    await session.execute(
                        select(CompanyOntologyObject).where(
                            CompanyOntologyObject.tenant_id == tenant_id,
                            CompanyOntologyObject.release_id == release.supersedes_release_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            baseline_objects = {item.stable_object_key: item.id for item in previous}
        object_map: dict[str, CompanyOntologyObject] = {}
        materialized: list[tuple[str, Any, list[str], str]] = []
        for item in candidate.objects:
            row = CompanyOntologyObject(
                tenant_id=tenant_id,
                stable_object_key=item.stable_object_key,
                namespace=namespace,
                object_type_ref=item.object_type_ref,
                display_name=item.display_name,
                properties_json=dict(item.properties),
                release_id=release.id,
                valid_from=item.valid_from,
                valid_until=item.valid_until,
                observed_at=item.observed_at,
                source_refs_json=list(item.source_refs),
                sensitivity=item.sensitivity,
                permission_resource_ref=item.permission_resource_ref,
                status="active",
                supersedes_object_id=baseline_objects.get(item.stable_object_key),
            )
            session.add(row)
            object_map[item.stable_object_key] = row
            materialized.append(("object", row, list(item.source_refs), item.stable_object_key))
        await session.flush()
        for item in candidate.objects:
            row = object_map[item.stable_object_key]
            for identity in item.source_identities:
                session.add(
                    CompanyOntologyObjectIdentity(
                        tenant_id=tenant_id,
                        object_id=row.id,
                        source_contract_id=identity.source_contract_id,
                        source_identity_key=identity.source_identity_key,
                        aliases_json=list(identity.aliases),
                        lineage_json={
                            "release_id": str(release.id),
                            "supersedes_object_id": (
                                str(row.supersedes_object_id) if row.supersedes_object_id else None
                            ),
                        },
                        curation_run_id=run.id,
                        status="active",
                    )
                )
        for item in candidate.assertions:
            row = CompanyOntologyAssertion(
                tenant_id=tenant_id,
                stable_assertion_key=item.stable_assertion_key,
                subject_object_id=object_map[item.subject_key].id,
                predicate_ref=item.predicate_ref,
                object_id=object_map[item.object_key].id if item.object_key else None,
                typed_value_json=({"value": _jsonable(item.typed_value)} if item.object_key is None else None),
                assertion_kind=item.assertion_kind,
                valid_from=item.valid_from,
                valid_until=item.valid_until,
                observed_at=item.observed_at,
                evidence_bundle_refs_json=list(item.source_refs),
                derived_by_rule_ref=item.derived_by_rule_ref,
                curation_run_id=run.id,
                release_id=release.id,
                sensitivity=item.sensitivity,
                permission_resource_ref=item.permission_resource_ref,
                status="active",
                supersedes_assertion_id=item.supersedes_assertion_id,
            )
            session.add(row)
            materialized.append(("assertion", row, list(item.source_refs), item.stable_assertion_key))
        for item in candidate.links:
            row = CompanyOntologyLink(
                tenant_id=tenant_id,
                stable_link_key=item.stable_link_key,
                link_type_ref=item.link_type_ref,
                from_object_id=object_map[item.from_object_key].id,
                to_object_id=object_map[item.to_object_key].id,
                properties_json=dict(item.properties),
                valid_from=item.valid_from,
                valid_until=item.valid_until,
                observed_at=item.observed_at,
                evidence_bundle_refs_json=list(item.source_refs),
                curation_run_id=run.id,
                release_id=release.id,
                sensitivity=item.sensitivity,
                permission_resource_ref=item.permission_resource_ref,
                status="active",
                supersedes_link_id=item.supersedes_link_id,
            )
            session.add(row)
            materialized.append(("link", row, list(item.source_refs), item.stable_link_key))
        for item in candidate.events:
            row = CompanyOntologyEvent(
                tenant_id=tenant_id,
                stable_event_key=item.stable_event_key,
                event_type_ref=item.event_type_ref,
                subject_object_id=(object_map[item.subject_object_key].id if item.subject_object_key else None),
                payload_json=dict(item.payload),
                occurred_at=item.occurred_at,
                effective_from=item.effective_from,
                effective_until=item.effective_until,
                observed_at=item.observed_at,
                sequence=item.sequence,
                evidence_bundle_refs_json=list(item.source_refs),
                curation_run_id=run.id,
                release_id=release.id,
                sensitivity=item.sensitivity,
                permission_resource_ref=item.permission_resource_ref,
                status="active",
            )
            session.add(row)
            materialized.append(("event", row, list(item.source_refs), item.stable_event_key))
        await session.flush()
        for kind, row, source_refs, stable_key in materialized:
            session.add(
                CompanyOntologyReleaseItem(
                    tenant_id=tenant_id,
                    release_id=release.id,
                    item_kind=kind,
                    item_id=row.id,
                    item_hash=_hash(
                        {
                            "kind": kind,
                            "stable_key": stable_key,
                            "release_id": release.id,
                            "source_refs": source_refs,
                        }
                    ),
                    source_refs_json=source_refs,
                )
            )
            for source_ref in sorted(set(source_refs)):
                evidence = (
                    await session.execute(
                        select(CompanyKnowledgeEvidence).where(
                            CompanyKnowledgeEvidence.id == _evidence_id(source_ref),
                            CompanyKnowledgeEvidence.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one()
                session.add(
                    CompanyOntologyEvidenceBinding(
                        tenant_id=tenant_id,
                        subject_kind=kind,
                        subject_id=row.id,
                        bundle_key=stable_key,
                        support_mode="joint",
                        evidence_id=evidence.id,
                        source_acl_snapshot_hash=evidence.source_acl_snapshot_hash,
                        status="active",
                    )
                )
        await session.flush()

    @staticmethod
    async def _review_receipts(
        session: Any,
        *,
        proposal: CompanyKnowledgeProposal,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        reviews = (
            (
                await session.execute(
                    select(CompanyKnowledgeReview)
                    .where(
                        CompanyKnowledgeReview.tenant_id == proposal.tenant_id,
                        CompanyKnowledgeReview.proposal_id == proposal.id,
                    )
                    .order_by(
                        CompanyKnowledgeReview.created_at,
                        CompanyKnowledgeReview.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        receipts = [
            {
                "review_id": str(row.id),
                "reviewer_user_id": str(row.reviewer_user_id),
                "reviewer_role": row.reviewer_role,
                "review_round": row.review_round,
                "decision": row.decision,
                "decision_hash": row.decision_hash,
                "evidence_refs": list(row.evidence_refs_json or []),
            }
            for row in reviews
        ]
        evaluation = evaluate_company_review_set(
            receipts,
            policy=dict(proposal.required_review_policy_json or {}),
            created_by_type=proposal.created_by_type,
            created_by_id=proposal.created_by_id,
            risk_level=proposal.risk_level,
        )
        return evaluation, receipts

    @staticmethod
    async def _proposal_for_run(
        session: Any,
        *,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        for_update: bool = False,
    ) -> CompanyKnowledgeProposal | None:
        statement = select(CompanyKnowledgeProposal).where(
            CompanyKnowledgeProposal.tenant_id == tenant_id,
            CompanyKnowledgeProposal.ontology_mapping_json["curation_run_id"].astext == str(run_id),
        )
        if for_update:
            statement = statement.with_for_update()
        return (await session.execute(statement)).scalar_one_or_none()

    @staticmethod
    async def _locked_release(
        session: Any,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
    ) -> CompanyOntologyRelease:
        release = (
            await session.execute(
                select(CompanyOntologyRelease)
                .where(
                    CompanyOntologyRelease.id == release_id,
                    CompanyOntologyRelease.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if release is None:
            raise LookupError("company_ontology_release_not_found")
        return release

    @staticmethod
    def _candidate_source_refs(candidate: OntologyCandidatePatch) -> list[str]:
        values = {
            source_ref
            for item in (
                *candidate.objects,
                *candidate.assertions,
                *candidate.links,
                *candidate.events,
                *candidate.definition_overrides.object_types,
                *candidate.definition_overrides.property_types,
                *candidate.definition_overrides.link_types,
                *candidate.definition_overrides.event_types,
            )
            for source_ref in item.source_refs
        }
        values.update(source_ref for receipt in candidate.model_prompt_receipts for source_ref in receipt.source_refs)
        return sorted(values)

    @staticmethod
    def _candidate_sensitivity(candidate: OntologyCandidatePatch) -> str:
        values = [
            canonicalize_sensitivity(item.sensitivity).value
            for item in (
                *candidate.objects,
                *candidate.assertions,
                *candidate.links,
                *candidate.events,
                *candidate.definition_overrides.object_types,
                *candidate.definition_overrides.property_types,
                *candidate.definition_overrides.link_types,
                *candidate.definition_overrides.event_types,
            )
        ]
        return max(values, key=sensitivity_rank) if values else "PL1_public"


__all__ = [
    "CompanyOntologyService",
    "OntologyActivationRequest",
    "OntologyCurationRequest",
    "OntologyCurationResult",
    "OntologyPackageInstallRequest",
    "OntologyReleaseLifecycleRequest",
]
