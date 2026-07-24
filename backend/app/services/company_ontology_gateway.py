"""Permission-first Company Ontology read, lineage, and simulation gateway."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select

from app.models.company_knowledge import CompanyKnowledgeEvidence
from app.models.company_ontology import (
    CompanyOntologyActionType,
    CompanyOntologyAssertion,
    CompanyOntologyEvidenceBinding,
    CompanyOntologyLink,
    CompanyOntologyObject,
    CompanyOntologyObjectType,
    CompanyOntologyPackageVersion,
    CompanyOntologyRelease,
    CompanyOntologyReleaseItem,
)
from app.services.company_knowledge_evidence import (
    CompanyKnowledgeEventInput,
    append_company_knowledge_event,
)
from app.services.company_knowledge_permissions import (
    CompanyKnowledgePrincipal,
    CompanyKnowledgeResource,
    resolve_company_knowledge_permission,
)
from app.services.company_ontology_contracts import OntologyActionDefinition
from app.services.company_ontology_engine import (
    OntologyEnginePlugin,
    ReferenceOntologyEngine,
    validate_typed_payload,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class OntologyQueryRequest:
    namespaces: tuple[str, ...] = ()
    query_ref: str | None = None
    query_input: dict[str, Any] | None = None
    object_type_refs: tuple[str, ...] = ()
    object_ids: tuple[uuid.UUID, ...] = ()
    limit: int = 50
    include_facts: bool = True
    include_links: bool = True
    trace_id: str = ""


@dataclass(frozen=True, slots=True)
class OntologyObjectReadRequest:
    object_id: uuid.UUID
    include_facts: bool = True
    include_links: bool = True
    trace_id: str = ""


@dataclass(frozen=True, slots=True)
class OntologyFactExplainRequest:
    assertion_id: uuid.UUID
    trace_id: str = ""


@dataclass(frozen=True, slots=True)
class OntologyActionSimulationRequest:
    action_type_ref: str
    proposed_input: dict[str, Any]
    namespace: str | None = None
    trace_id: str = ""


@dataclass(frozen=True, slots=True)
class OntologyGatewayResult:
    status: str
    payload: dict[str, Any]
    authority: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            **self.payload,
            "authority": self.authority,
            "warnings": list(self.warnings),
        }


class CompanyOntologyGateway:
    """Reads only active release membership and authorizes before expansion."""

    def __init__(self, *, engine: OntologyEnginePlugin | None = None) -> None:
        self._engine = engine or ReferenceOntologyEngine()

    async def query(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: OntologyQueryRequest,
    ) -> OntologyGatewayResult:
        limit = max(1, min(int(request.limit), 200))
        now = _utcnow()
        release_statement = select(CompanyOntologyRelease).where(
            CompanyOntologyRelease.tenant_id == principal.tenant_id,
            CompanyOntologyRelease.status == "active",
            CompanyOntologyRelease.valid_from <= now,
            or_(
                CompanyOntologyRelease.valid_until.is_(None),
                CompanyOntologyRelease.valid_until > now,
            ),
        )
        if request.namespaces:
            release_statement = release_statement.where(CompanyOntologyRelease.namespace.in_(request.namespaces))
        releases = (await session.execute(release_statement.order_by(CompanyOntologyRelease.namespace))).scalars().all()
        authorized_releases: list[CompanyOntologyRelease] = []
        namespace_receipts: list[dict[str, Any]] = []
        for release in releases:
            package_version = (
                await session.execute(
                    select(CompanyOntologyPackageVersion).where(
                        CompanyOntologyPackageVersion.id == release.package_version_id,
                        CompanyOntologyPackageVersion.tenant_id == principal.tenant_id,
                    )
                )
            ).scalar_one()
            decision = await resolve_company_knowledge_permission(
                session,
                principal=principal,
                resource=CompanyKnowledgeResource(
                    tenant_id=principal.tenant_id,
                    resource_type="company_ontology_namespace",
                    resource_id=None,
                    resource_key=f"namespace:{release.namespace}",
                    namespace=release.namespace,
                    sensitivity="PL1_public",
                    source_acl_snapshot_hash=package_version.content_hash,
                    source_acl={"all_tenant_members": True},
                    evidence_access_complete=True,
                    publication_status="active",
                    validity_active=True,
                ),
                action="query",
            )
            await self._audit_permission(
                session,
                principal=principal,
                decision=decision.evidence(),
                allowed=decision.allowed,
                resource_type="ontology_namespace",
                resource_id=release.id,
                resource_version=release.version,
                trace_id=request.trace_id,
                event_suffix=f"namespace:{release.id}",
            )
            if decision.allowed:
                authorized_releases.append(release)
                namespace_receipts.append(decision.evidence())
        if not authorized_releases:
            return OntologyGatewayResult(
                status="empty",
                payload={"objects": [], "result_count": 0, "truncated": False},
            )

        if request.query_ref:
            await self._validate_named_query(
                session,
                tenant_id=principal.tenant_id,
                releases=authorized_releases,
                query_ref=request.query_ref,
                query_input=dict(request.query_input or {}),
            )
        release_ids = [release.id for release in authorized_releases]
        statement = (
            select(CompanyOntologyObject, CompanyOntologyRelease)
            .join(
                CompanyOntologyRelease,
                CompanyOntologyRelease.id == CompanyOntologyObject.release_id,
            )
            .join(
                CompanyOntologyReleaseItem,
                and_(
                    CompanyOntologyReleaseItem.tenant_id == CompanyOntologyObject.tenant_id,
                    CompanyOntologyReleaseItem.release_id == CompanyOntologyObject.release_id,
                    CompanyOntologyReleaseItem.item_kind == "object",
                    CompanyOntologyReleaseItem.item_id == CompanyOntologyObject.id,
                ),
            )
            .where(
                CompanyOntologyObject.tenant_id == principal.tenant_id,
                CompanyOntologyObject.release_id.in_(release_ids),
                CompanyOntologyObject.status == "active",
                CompanyOntologyObject.valid_from <= now,
                or_(
                    CompanyOntologyObject.valid_until.is_(None),
                    CompanyOntologyObject.valid_until > now,
                ),
            )
            .order_by(
                CompanyOntologyObject.object_type_ref,
                CompanyOntologyObject.stable_object_key,
            )
            .limit(500)
        )
        if request.object_type_refs:
            statement = statement.where(CompanyOntologyObject.object_type_ref.in_(request.object_type_refs))
        if request.object_ids:
            statement = statement.where(CompanyOntologyObject.id.in_(request.object_ids))
        candidates = (await session.execute(statement)).all()
        authorized: list[tuple[CompanyOntologyObject, CompanyOntologyRelease, dict[str, Any]]] = []
        for obj, release in candidates:
            decision = await self._authorize_item(
                session,
                principal=principal,
                action="query",
                item=obj,
                release=release,
                source_refs=tuple(obj.source_refs_json or []),
                resource_type="company_ontology_object",
                resource_key=obj.permission_resource_ref,
            )
            await self._audit_permission(
                session,
                principal=principal,
                decision=decision,
                allowed=bool(decision.get("allowed")),
                resource_type="ontology_object",
                resource_id=obj.id,
                resource_version=release.version,
                trace_id=request.trace_id,
                event_suffix=f"object:{obj.id}",
            )
            if decision.get("allowed"):
                authorized.append((obj, release, decision))

        base_objects = [
            {
                "object_id": str(obj.id),
                "stable_object_key": obj.stable_object_key,
                "namespace": obj.namespace,
                "object_type_ref": obj.object_type_ref,
                "display_name": obj.display_name,
                "properties": dict(obj.properties_json or {}),
                "release_id": str(release.id),
                "release_version": release.version,
                "valid_from": obj.valid_from.isoformat(),
                "valid_until": obj.valid_until.isoformat() if obj.valid_until else None,
                "observed_at": obj.observed_at.isoformat(),
                "sensitivity": obj.sensitivity,
                "source_refs": list(obj.source_refs_json or []),
                "facts": [],
                "links": [],
            }
            for obj, release, _decision in authorized
        ]
        engine_result = await self._engine.query(
            {
                "objects": base_objects,
                "object_type_refs": list(request.object_type_refs),
                "object_ids": [str(value) for value in request.object_ids],
                "query_ref": request.query_ref,
                "query_input": dict(request.query_input or {}),
                "limit": limit,
            }
        )
        result_objects = list(engine_result["objects"])
        allowed_ids = {uuid.UUID(item["object_id"]) for item in result_objects}
        if request.include_facts and allowed_ids:
            facts = await self._authorized_facts(
                session,
                principal=principal,
                release_ids=release_ids,
                object_ids=allowed_ids,
                trace_id=request.trace_id,
            )
            by_subject: dict[str, list[dict[str, Any]]] = {}
            for fact in facts:
                by_subject.setdefault(fact["subject_object_id"], []).append(fact)
            for item in result_objects:
                item["facts"] = by_subject.get(item["object_id"], [])
        if request.include_links and allowed_ids:
            links = await self._authorized_links(
                session,
                principal=principal,
                release_ids=release_ids,
                object_ids=allowed_ids,
                trace_id=request.trace_id,
            )
            for item in result_objects:
                item["links"] = [
                    link for link in links if item["object_id"] in {link["from_object_id"], link["to_object_id"]}
                ]
        authority = {
            "schema": "hive.company_ontology_query_authority.v1",
            "namespace_decisions": namespace_receipts,
            "authorized_release_ids": [str(release.id) for release in authorized_releases],
            "fresh_permission_per_item": True,
            "authorization_before_expansion": True,
        }
        return OntologyGatewayResult(
            status="ok" if result_objects else "empty",
            payload={
                "objects": result_objects,
                "result_count": len(result_objects),
                "truncated": bool(engine_result.get("truncated")),
                "query_receipt": engine_result.get("query_receipt"),
            },
            authority=authority,
        )

    async def get_object(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: OntologyObjectReadRequest,
    ) -> OntologyGatewayResult:
        result = await self.query(
            session,
            principal=principal,
            request=OntologyQueryRequest(
                object_ids=(request.object_id,),
                limit=1,
                include_facts=request.include_facts,
                include_links=request.include_links,
                trace_id=request.trace_id,
            ),
        )
        objects = list(result.payload.get("objects") or [])
        if not objects:
            return OntologyGatewayResult(
                status="not_found_or_denied",
                payload={"object": None},
            )
        return OntologyGatewayResult(
            status="ok",
            payload={"object": objects[0]},
            authority=result.authority,
        )

    async def explain_fact(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: OntologyFactExplainRequest,
    ) -> OntologyGatewayResult:
        now = _utcnow()
        row = (
            await session.execute(
                select(CompanyOntologyAssertion, CompanyOntologyRelease)
                .join(
                    CompanyOntologyRelease,
                    CompanyOntologyRelease.id == CompanyOntologyAssertion.release_id,
                )
                .join(
                    CompanyOntologyReleaseItem,
                    and_(
                        CompanyOntologyReleaseItem.tenant_id == CompanyOntologyAssertion.tenant_id,
                        CompanyOntologyReleaseItem.release_id == CompanyOntologyAssertion.release_id,
                        CompanyOntologyReleaseItem.item_kind == "assertion",
                        CompanyOntologyReleaseItem.item_id == CompanyOntologyAssertion.id,
                    ),
                )
                .where(
                    CompanyOntologyAssertion.id == request.assertion_id,
                    CompanyOntologyAssertion.tenant_id == principal.tenant_id,
                    CompanyOntologyAssertion.status == "active",
                    CompanyOntologyRelease.status == "active",
                    CompanyOntologyRelease.valid_from <= now,
                    or_(
                        CompanyOntologyRelease.valid_until.is_(None),
                        CompanyOntologyRelease.valid_until > now,
                    ),
                )
            )
        ).one_or_none()
        if row is None:
            return OntologyGatewayResult(
                status="not_found_or_denied",
                payload={"fact": None},
            )
        assertion, release = row
        decision = await self._authorize_item(
            session,
            principal=principal,
            action="read",
            item=assertion,
            release=release,
            source_refs=tuple(assertion.evidence_bundle_refs_json or []),
            resource_type="company_ontology_assertion",
            resource_key=assertion.permission_resource_ref,
        )
        await self._audit_permission(
            session,
            principal=principal,
            decision=decision,
            allowed=bool(decision.get("allowed")),
            resource_type="ontology_assertion",
            resource_id=assertion.id,
            resource_version=release.version,
            trace_id=request.trace_id,
            event_suffix=f"fact:{assertion.id}",
        )
        if not decision.get("allowed"):
            return OntologyGatewayResult(
                status="not_found_or_denied",
                payload={"fact": None},
            )
        bindings = (
            await session.execute(
                select(
                    CompanyOntologyEvidenceBinding,
                    CompanyKnowledgeEvidence,
                )
                .join(
                    CompanyKnowledgeEvidence,
                    CompanyKnowledgeEvidence.id == CompanyOntologyEvidenceBinding.evidence_id,
                )
                .where(
                    CompanyOntologyEvidenceBinding.tenant_id == principal.tenant_id,
                    CompanyOntologyEvidenceBinding.subject_kind == "assertion",
                    CompanyOntologyEvidenceBinding.subject_id == assertion.id,
                    CompanyOntologyEvidenceBinding.status == "active",
                    CompanyKnowledgeEvidence.tenant_id == principal.tenant_id,
                    CompanyKnowledgeEvidence.status == "accepted",
                )
            )
        ).all()
        evidence = [
            {
                "evidence_ref": f"company-evidence://{row.id}",
                "content_hash": row.content_hash,
                "source_acl_snapshot_hash": row.source_acl_snapshot_hash,
                "source_contract_id": str(row.source_contract_id),
                "source_contract_version": row.source_contract_version,
                "observed_at": row.observed_at.isoformat(),
                "support_mode": binding.support_mode,
                "bundle_key": binding.bundle_key,
            }
            for binding, row in bindings
        ]
        lineage = await self._engine.resolve_fact_lineage(
            {
                "assertion_id": assertion.id,
                "release_id": release.id,
                "source_refs": list(assertion.evidence_bundle_refs_json or []),
                "evidence": evidence,
                "coverage": {
                    "complete": bool(evidence),
                    "evidence_count": len(evidence),
                },
            }
        )
        return OntologyGatewayResult(
            status="ok",
            payload={
                "fact": {
                    "assertion_id": str(assertion.id),
                    "stable_assertion_key": assertion.stable_assertion_key,
                    "subject_object_id": str(assertion.subject_object_id),
                    "predicate_ref": assertion.predicate_ref,
                    "object_id": (str(assertion.object_id) if assertion.object_id else None),
                    "typed_value": (
                        dict(assertion.typed_value_json or {}).get("value")
                        if assertion.typed_value_json is not None
                        else None
                    ),
                    "assertion_kind": assertion.assertion_kind,
                    "derived_by_rule_ref": assertion.derived_by_rule_ref,
                    "valid_from": assertion.valid_from.isoformat(),
                    "valid_until": (assertion.valid_until.isoformat() if assertion.valid_until else None),
                    "source_refs": list(assertion.evidence_bundle_refs_json or []),
                    "sensitivity": assertion.sensitivity,
                    "lineage": lineage,
                }
            },
            authority=decision,
        )

    async def simulate_action(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: OntologyActionSimulationRequest,
    ) -> OntologyGatewayResult:
        now = _utcnow()
        statement = (
            select(CompanyOntologyActionType, CompanyOntologyRelease)
            .join(
                CompanyOntologyRelease,
                CompanyOntologyRelease.id == CompanyOntologyActionType.release_id,
            )
            .join(
                CompanyOntologyReleaseItem,
                and_(
                    CompanyOntologyReleaseItem.tenant_id == CompanyOntologyActionType.tenant_id,
                    CompanyOntologyReleaseItem.release_id == CompanyOntologyActionType.release_id,
                    CompanyOntologyReleaseItem.item_kind == "action_type",
                    CompanyOntologyReleaseItem.item_id == CompanyOntologyActionType.id,
                ),
            )
            .where(
                CompanyOntologyActionType.tenant_id == principal.tenant_id,
                CompanyOntologyActionType.action_type_ref == request.action_type_ref,
                CompanyOntologyActionType.status == "active",
                CompanyOntologyRelease.status == "active",
                CompanyOntologyRelease.valid_from <= now,
                or_(
                    CompanyOntologyRelease.valid_until.is_(None),
                    CompanyOntologyRelease.valid_until > now,
                ),
            )
        )
        if request.namespace:
            statement = statement.where(CompanyOntologyRelease.namespace == request.namespace)
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            return OntologyGatewayResult(
                status="not_found_or_denied",
                payload={"simulation": None},
            )
        action, release = row
        decision = await resolve_company_knowledge_permission(
            session,
            principal=principal,
            resource=CompanyKnowledgeResource(
                tenant_id=principal.tenant_id,
                resource_type="company_ontology_action",
                resource_id=action.id,
                resource_key=action.permission_resource_ref,
                namespace=release.namespace,
                sensitivity=action.sensitivity,
                source_acl_snapshot_hash=None,
                source_acl=None,
                evidence_access_complete=True,
                publication_status="active",
                validity_active=True,
            ),
            action="simulate",
        )
        await self._audit_permission(
            session,
            principal=principal,
            decision=decision.evidence(),
            allowed=decision.allowed,
            resource_type="ontology_action",
            resource_id=action.id,
            resource_version=release.version,
            trace_id=request.trace_id,
            event_suffix=f"simulate:{action.id}",
        )
        if not decision.allowed:
            return OntologyGatewayResult(
                status="not_found_or_denied",
                payload={"simulation": None},
            )
        definition = OntologyActionDefinition.model_validate(
            {
                "action_type_ref": action.action_type_ref,
                "display_name": action.action_type_ref,
                "description": action.action_type_ref,
                "input_schema": dict(action.input_schema_json or {}),
                "output_schema": dict(action.output_schema_json or {}),
                "required_capability": action.required_capability,
                "tool_workflow_mapping": dict(action.tool_workflow_mapping_json or {}),
                "approval_policy": dict(action.approval_policy_json or {}),
                "side_effect_classification": action.side_effect_classification,
                "simulation_contract": dict(action.simulation_contract_json or {}),
                "source_refs": list(action.source_refs_json or []),
                "sensitivity": action.sensitivity,
                "permission_resource_ref": action.permission_resource_ref,
            }
        )
        simulation = await self._engine.simulate_action(
            action_definition=definition,
            proposed_input=dict(request.proposed_input),
        )
        return OntologyGatewayResult(
            status=simulation["status"],
            payload={"simulation": simulation},
            authority=decision.evidence(),
        )

    async def list_types(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        namespaces: tuple[str, ...] = (),
        trace_id: str,
    ) -> OntologyGatewayResult:
        query = await self.query(
            session,
            principal=principal,
            request=OntologyQueryRequest(
                namespaces=namespaces,
                limit=1,
                include_facts=False,
                include_links=False,
                trace_id=trace_id,
            ),
        )
        release_ids = [uuid.UUID(value) for value in (query.authority or {}).get("authorized_release_ids", [])]
        if not release_ids:
            return OntologyGatewayResult(
                status="empty",
                payload={"types": []},
                authority=query.authority,
            )
        rows = (
            await session.execute(
                select(
                    CompanyOntologyObjectType,
                    CompanyOntologyRelease,
                    CompanyOntologyPackageVersion,
                )
                .select_from(CompanyOntologyObjectType)
                .join(
                    CompanyOntologyReleaseItem,
                    and_(
                        CompanyOntologyReleaseItem.tenant_id == CompanyOntologyObjectType.tenant_id,
                        CompanyOntologyReleaseItem.release_id == CompanyOntologyObjectType.release_id,
                        CompanyOntologyReleaseItem.item_kind == "object_type",
                        CompanyOntologyReleaseItem.item_id == CompanyOntologyObjectType.id,
                    ),
                )
                .join(
                    CompanyOntologyRelease,
                    CompanyOntologyRelease.id == CompanyOntologyObjectType.release_id,
                )
                .join(
                    CompanyOntologyPackageVersion,
                    CompanyOntologyPackageVersion.id == CompanyOntologyRelease.package_version_id,
                )
                .where(
                    CompanyOntologyObjectType.tenant_id == principal.tenant_id,
                    CompanyOntologyObjectType.release_id.in_(release_ids),
                    CompanyOntologyObjectType.status == "active",
                )
                .order_by(CompanyOntologyObjectType.type_ref)
            )
        ).all()
        visible: list[CompanyOntologyObjectType] = []
        type_decisions: list[dict[str, Any]] = []
        for row, release, package_version in rows:
            decision = await resolve_company_knowledge_permission(
                session,
                principal=principal,
                resource=CompanyKnowledgeResource(
                    tenant_id=principal.tenant_id,
                    resource_type="company_ontology_object_type",
                    resource_id=row.id,
                    resource_key=row.permission_resource_ref,
                    namespace=release.namespace,
                    sensitivity=row.sensitivity,
                    source_acl_snapshot_hash=package_version.content_hash,
                    source_acl={"all_tenant_members": True},
                    evidence_access_complete=True,
                    publication_status=release.status,
                    validity_active=True,
                ),
                action="query",
            )
            await self._audit_permission(
                session,
                principal=principal,
                decision=decision.evidence(),
                allowed=decision.allowed,
                resource_type="ontology_object_type",
                resource_id=row.id,
                resource_version=release.version,
                trace_id=trace_id,
                event_suffix=f"type:{row.id}",
            )
            if decision.allowed:
                visible.append(row)
                type_decisions.append(decision.evidence())
        authority = dict(query.authority or {})
        authority["type_decisions"] = type_decisions
        return OntologyGatewayResult(
            status="ok" if visible else "empty",
            payload={
                "types": [
                    {
                        "type_id": str(row.id),
                        "release_id": str(row.release_id),
                        "namespace": row.namespace,
                        "type_ref": row.type_ref,
                        "schema": dict(row.schema_json or {}),
                        "sensitivity": row.sensitivity,
                    }
                    for row in visible
                ]
            },
            authority=authority,
        )

    async def _validate_named_query(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        releases: list[CompanyOntologyRelease],
        query_ref: str,
        query_input: dict[str, Any],
    ) -> None:
        definitions: list[dict[str, Any]] = []
        for release in releases:
            version = (
                await session.execute(
                    select(CompanyOntologyPackageVersion).where(
                        CompanyOntologyPackageVersion.id == release.package_version_id,
                        CompanyOntologyPackageVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one()
            definitions.extend(item for item in list(version.queries_json or []) if item.get("query_ref") == query_ref)
        if not definitions:
            raise LookupError("company_ontology_named_query_not_found")
        errors = validate_typed_payload(
            dict(definitions[0].get("input_schema") or {}),
            query_input,
        )
        if errors:
            raise ValueError("company_ontology_named_query_input_invalid:" + ",".join(errors))

    async def _authorize_item(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        action: str,
        item: Any,
        release: CompanyOntologyRelease,
        source_refs: tuple[str, ...],
        resource_type: str,
        resource_key: str,
    ) -> dict[str, Any]:
        if not source_refs:
            return {
                "schema": "hive.company_knowledge_permission_decision.v1",
                "allowed": False,
                "deny_reason_code": "complete_evidence_bundle_required",
            }
        receipts: list[dict[str, Any]] = []
        for source_ref in source_refs:
            try:
                evidence_id = uuid.UUID(str(source_ref).removeprefix("company-evidence://").split("#", 1)[0])
            except ValueError:
                return {
                    "schema": "hive.company_knowledge_permission_decision.v1",
                    "allowed": False,
                    "deny_reason_code": "evidence_reference_invalid",
                }
            evidence = (
                await session.execute(
                    select(CompanyKnowledgeEvidence).where(
                        CompanyKnowledgeEvidence.id == evidence_id,
                        CompanyKnowledgeEvidence.tenant_id == principal.tenant_id,
                        CompanyKnowledgeEvidence.status == "accepted",
                    )
                )
            ).scalar_one_or_none()
            if evidence is None:
                return {
                    "schema": "hive.company_knowledge_permission_decision.v1",
                    "allowed": False,
                    "deny_reason_code": "evidence_unavailable",
                }
            decision = await resolve_company_knowledge_permission(
                session,
                principal=principal,
                resource=CompanyKnowledgeResource(
                    tenant_id=principal.tenant_id,
                    resource_type=resource_type,
                    resource_id=item.id,
                    resource_key=resource_key,
                    namespace=release.namespace,
                    sensitivity=item.sensitivity,
                    source_acl_snapshot_hash=evidence.source_acl_snapshot_hash,
                    source_acl=dict(evidence.source_acl_snapshot_json or {}),
                    evidence_access_complete=bool(dict(evidence.coverage_ledger_json or {}).get("complete")),
                    publication_status=release.status,
                    validity_active=True,
                ),
                action=action,
            )
            receipts.append(decision.evidence())
            if not decision.allowed:
                return decision.evidence()
        return {
            "schema": "hive.company_ontology_composite_permission.v1",
            "allowed": True,
            "requested_action": action,
            "evidence_decisions": receipts,
        }

    async def _authorized_facts(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        release_ids: list[uuid.UUID],
        object_ids: set[uuid.UUID],
        trace_id: str,
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(CompanyOntologyAssertion, CompanyOntologyRelease)
                .join(
                    CompanyOntologyRelease,
                    CompanyOntologyRelease.id == CompanyOntologyAssertion.release_id,
                )
                .join(
                    CompanyOntologyReleaseItem,
                    and_(
                        CompanyOntologyReleaseItem.tenant_id == CompanyOntologyAssertion.tenant_id,
                        CompanyOntologyReleaseItem.release_id == CompanyOntologyAssertion.release_id,
                        CompanyOntologyReleaseItem.item_kind == "assertion",
                        CompanyOntologyReleaseItem.item_id == CompanyOntologyAssertion.id,
                    ),
                )
                .where(
                    CompanyOntologyAssertion.tenant_id == principal.tenant_id,
                    CompanyOntologyAssertion.release_id.in_(release_ids),
                    CompanyOntologyAssertion.subject_object_id.in_(object_ids),
                    or_(
                        CompanyOntologyAssertion.object_id.is_(None),
                        CompanyOntologyAssertion.object_id.in_(object_ids),
                    ),
                    CompanyOntologyAssertion.status == "active",
                )
            )
        ).all()
        results: list[dict[str, Any]] = []
        for assertion, release in rows:
            decision = await self._authorize_item(
                session,
                principal=principal,
                action="query",
                item=assertion,
                release=release,
                source_refs=tuple(assertion.evidence_bundle_refs_json or []),
                resource_type="company_ontology_assertion",
                resource_key=assertion.permission_resource_ref,
            )
            if not decision.get("allowed"):
                await self._audit_permission(
                    session,
                    principal=principal,
                    decision=decision,
                    allowed=False,
                    resource_type="ontology_assertion",
                    resource_id=assertion.id,
                    resource_version=release.version,
                    trace_id=trace_id,
                    event_suffix=f"fact-query:{assertion.id}",
                )
                continue
            results.append(
                {
                    "assertion_id": str(assertion.id),
                    "stable_assertion_key": assertion.stable_assertion_key,
                    "subject_object_id": str(assertion.subject_object_id),
                    "predicate_ref": assertion.predicate_ref,
                    "object_id": (str(assertion.object_id) if assertion.object_id else None),
                    "typed_value": (
                        dict(assertion.typed_value_json or {}).get("value")
                        if assertion.typed_value_json is not None
                        else None
                    ),
                    "assertion_kind": assertion.assertion_kind,
                    "valid_from": assertion.valid_from.isoformat(),
                    "valid_until": (assertion.valid_until.isoformat() if assertion.valid_until else None),
                    "source_refs": list(assertion.evidence_bundle_refs_json or []),
                    "sensitivity": assertion.sensitivity,
                }
            )
        return results

    async def _authorized_links(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        release_ids: list[uuid.UUID],
        object_ids: set[uuid.UUID],
        trace_id: str,
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(CompanyOntologyLink, CompanyOntologyRelease)
                .join(
                    CompanyOntologyRelease,
                    CompanyOntologyRelease.id == CompanyOntologyLink.release_id,
                )
                .join(
                    CompanyOntologyReleaseItem,
                    and_(
                        CompanyOntologyReleaseItem.tenant_id == CompanyOntologyLink.tenant_id,
                        CompanyOntologyReleaseItem.release_id == CompanyOntologyLink.release_id,
                        CompanyOntologyReleaseItem.item_kind == "link",
                        CompanyOntologyReleaseItem.item_id == CompanyOntologyLink.id,
                    ),
                )
                .where(
                    CompanyOntologyLink.tenant_id == principal.tenant_id,
                    CompanyOntologyLink.release_id.in_(release_ids),
                    CompanyOntologyLink.from_object_id.in_(object_ids),
                    CompanyOntologyLink.to_object_id.in_(object_ids),
                    CompanyOntologyLink.status == "active",
                )
            )
        ).all()
        results: list[dict[str, Any]] = []
        for link, release in rows:
            decision = await self._authorize_item(
                session,
                principal=principal,
                action="query",
                item=link,
                release=release,
                source_refs=tuple(link.evidence_bundle_refs_json or []),
                resource_type="company_ontology_link",
                resource_key=link.permission_resource_ref,
            )
            if not decision.get("allowed"):
                await self._audit_permission(
                    session,
                    principal=principal,
                    decision=decision,
                    allowed=False,
                    resource_type="ontology_link",
                    resource_id=link.id,
                    resource_version=release.version,
                    trace_id=trace_id,
                    event_suffix=f"link-query:{link.id}",
                )
                continue
            results.append(
                {
                    "link_id": str(link.id),
                    "stable_link_key": link.stable_link_key,
                    "link_type_ref": link.link_type_ref,
                    "from_object_id": str(link.from_object_id),
                    "to_object_id": str(link.to_object_id),
                    "properties": dict(link.properties_json or {}),
                    "valid_from": link.valid_from.isoformat(),
                    "valid_until": (link.valid_until.isoformat() if link.valid_until else None),
                    "source_refs": list(link.evidence_bundle_refs_json or []),
                    "sensitivity": link.sensitivity,
                }
            )
        return results

    @staticmethod
    async def _audit_permission(
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        decision: dict[str, Any],
        allowed: bool,
        resource_type: str,
        resource_id: uuid.UUID,
        resource_version: int,
        trace_id: str,
        event_suffix: str,
    ) -> None:
        event_type = "company_knowledge.permission_allowed" if allowed else "company_knowledge.permission_denied"
        await append_company_knowledge_event(
            session,
            event_input=CompanyKnowledgeEventInput(
                tenant_id=principal.tenant_id,
                event_type=event_type,
                actor_type=principal.actor_type,
                actor_id=principal.actor_id,
                accountable_user_id=principal.accountable_user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_version=resource_version,
                source_refs=tuple(),
                source_hash=None,
                policy_snapshot=decision,
                trace_id=trace_id or f"company-ontology:{uuid.uuid4()}",
                request_id=None,
                idempotency_key=(f"company-ontology-permission:{trace_id or uuid.uuid4()}:{event_suffix}")[:300],
                outcome="allowed" if allowed else "denied",
                payload={
                    "allowed": allowed,
                    "deny_reason_code": decision.get("deny_reason_code"),
                },
                occurred_at=_utcnow(),
            ),
        )


__all__ = [
    "CompanyOntologyGateway",
    "OntologyActionSimulationRequest",
    "OntologyFactExplainRequest",
    "OntologyGatewayResult",
    "OntologyObjectReadRequest",
    "OntologyQueryRequest",
]
