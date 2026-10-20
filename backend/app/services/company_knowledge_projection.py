"""Publication-derived ontology; original documents remain independently usable."""

from dataclasses import replace
from datetime import datetime, timezone
import uuid

from sqlalchemy import select

from app.models.company_knowledge import CompanyKnowledgeProposal
from app.models.company_knowledge_analysis import CompanyKnowledgeAnalysisJob, CompanyKnowledgeExtraction
from app.models.company_ontology import CompanyOntologyActivation, CompanyOntologyCurationRun, CompanyOntologyRelease
from app.services.company_knowledge_analysis import (
    CompanyAnalysisGate,
    CompanyKnowledgeAnalysisReview,
    CompanyKnowledgeAnalyzer,
    analysis_hash,
    authorize_extraction_context,
)
from app.services.company_knowledge_evidence import append_company_knowledge_event
from app.services.company_ontology_contracts import (
    CANDIDATE_CONTRACT_VERSION,
    OntologyCandidatePatch,
    OntologyCandidateRejected,
    OntologySchemaBundle,
)
from app.services.company_ontology_service import CompanyOntologyService


class CompanyKnowledgeProjector:
    def __init__(self, *, data_root):
        self.analyzer = CompanyKnowledgeAnalyzer(data_root=data_root)
        self.ontology = CompanyOntologyService()

    @staticmethod
    async def retire(db, *, publication, at, status="retired"):
        releases = (
            await db.scalars(
                select(CompanyOntologyRelease)
                .where(
                    CompanyOntologyRelease.tenant_id == publication.tenant_id,
                    CompanyOntologyRelease.publication_id == publication.id,
                    CompanyOntologyRelease.status == "active",
                )
                .with_for_update()
            )
        ).all()
        for release in releases:
            release.status, release.valid_until, release.retired_at = status, at, at
        publication.graph_projection_status = status

    @staticmethod
    def candidate(*, extraction, publication, mapping):
        data = extraction.candidate_json
        all_refs = list(extraction.model_receipt_json["source_refs"])
        # Model citations express semantic support. Runtime context provenance is
        # conjunctive even when a model omits a comparison citation.
        common = {
            "source_refs": all_refs,
            "sensitivity": publication.sensitivity,
            "permission_resource_ref": publication.permission_resource_ref,
            "valid_from": publication.valid_from,
            "valid_until": publication.valid_until,
            "observed_at": publication.published_at,
        }
        prefix = f"publication:{publication.logical_resource_key}:"
        types, properties, links = (mapping.get(key, {}) for key in ("entity_types", "property_refs", "link_types"))

        def mapped_properties(values):
            mapped = {properties.get(key, key): value for key, value in values.items()}
            if len(mapped) != len(values):
                raise ValueError("company_projection_property_mapping_collision")
            return mapped

        return OntologyCandidatePatch.model_validate(
            {
                "schema_version": CANDIDATE_CONTRACT_VERSION,
                "snapshot_complete": True,
                "objects": [
                    {
                        **common,
                        "stable_object_key": prefix + item["key"],
                        "object_type_ref": types.get(item["type_ref"], item["type_ref"]),
                        "display_name": item["display_name"],
                        "properties": mapped_properties(item["properties"]),
                    }
                    for item in data["entities"]
                ],
                "assertions": [
                    {
                        **common,
                        "stable_assertion_key": prefix + item["key"],
                        "subject_key": prefix + item["subject_key"],
                        "predicate_ref": properties.get(item["predicate_ref"], item["predicate_ref"]),
                        "object_key": prefix + item["object_key"] if item["object_key"] else None,
                        "typed_value": item["typed_value"],
                        "assertion_kind": "sourced",
                    }
                    for item in data["assertions"]
                ],
                "links": [
                    {
                        **common,
                        "stable_link_key": prefix + item["key"],
                        "link_type_ref": links.get(item["type_ref"], item["type_ref"]),
                        "from_object_key": prefix + item["from_key"],
                        "to_object_key": prefix + item["to_key"],
                        "properties": item["properties"],
                    }
                    for item in data["links"]
                ],
                "coverage_ledger": {
                    "complete": True,
                    "total_units": len(all_refs),
                    "covered_units": len(all_refs),
                    "missing_units": [],
                },
                "conflict_ledger": {"unresolved": data["unresolved_conflicts"], "resolved": data["resolved_conflicts"]},
                "unresolved_questions": data["unresolved_questions"],
                "model_prompt_receipts": [
                    {
                        key: extraction.model_receipt_json[key]
                        for key in ("model", "prompt_hash", "response_hash", "source_refs")
                    }
                ],
            }
        )

    async def apply(self, db, *, principal, publication, proposal=None, confirmation=None, trace_id):
        proposal = proposal or await db.get(CompanyKnowledgeProposal, publication.proposal_id)
        binding = dict(proposal.ontology_mapping_json or {}).get("analysis") or {}
        override = dict(proposal.ontology_mapping_json or {}).get("analysis_override")
        if (
            override
            and override.get("subject_hash") == CompanyKnowledgeAnalysisReview.subject(proposal)
            and confirmation is None
        ):
            publication.graph_projection_status = "skipped_by_override"
            publication.graph_projection_receipt_json = {"override": override}
            return None
        extraction_id = publication.analysis_extraction_id or binding.get("extraction_id")
        if not extraction_id:
            publication.graph_projection_status = "not_analyzed"
            return None
        extraction = await db.scalar(
            select(CompanyKnowledgeExtraction).where(
                CompanyKnowledgeExtraction.tenant_id == publication.tenant_id,
                CompanyKnowledgeExtraction.id == uuid.UUID(str(extraction_id)),
                CompanyKnowledgeExtraction.validated.is_(True),
            )
        )
        if (
            extraction is None
            or extraction.input_hash != publication.content_hash
            or (binding and confirmation is None and binding.get("candidate_hash") != extraction.candidate_hash)
        ):
            raise CompanyAnalysisGate("company_projection_analysis_binding_changed")
        publication.analysis_extraction_id = extraction.id
        try:
            await authorize_extraction_context(db, principal=principal, extraction=extraction)
        except PermissionError:
            publication.graph_projection_status = "held_context_authority"
            publication.graph_projection_receipt_json = {
                "reason": "company_analysis_context_read_denied",
                "extraction_id": str(extraction.id),
                "candidate_hash": extraction.candidate_hash,
            }
            return None
        mapping = dict((confirmation or {}).get("mapping") or {})
        activation_query = select(CompanyOntologyActivation).where(
            CompanyOntologyActivation.tenant_id == publication.tenant_id, CompanyOntologyActivation.status == "active"
        )
        activation_query = (
            activation_query.where(CompanyOntologyActivation.id == uuid.UUID(confirmation["activation_id"]))
            if confirmation
            else activation_query.where(CompanyOntologyActivation.namespace == publication.namespace)
        )
        activation = await db.scalar(activation_query.with_for_update())
        base_receipt = {
            "schema": "hive.company_publication_projection.v1",
            "extraction_id": str(extraction.id),
            "candidate_hash": extraction.candidate_hash,
            "publication_id": str(publication.id),
            "content_hash": publication.content_hash,
            "confirmation": confirmation,
        }
        if activation is None:
            publication.graph_projection_status = "pending_types"
            publication.graph_projection_receipt_json = {**base_receipt, "reason": "active_schema_unavailable"}
            return None
        installation, version, bundle = await self.ontology._installation_bundle(
            db, tenant_id=publication.tenant_id, installation_id=activation.installation_id
        )
        if installation.status != "installed" or version.admission_status != "admitted":
            publication.graph_projection_status = "pending_types"
            publication.graph_projection_receipt_json = {**base_receipt, "reason": "installed_schema_unavailable"}
            return None
        if (
            activation.dry_run_receipt_json.get("passed") is not True
            or activation.dry_run_receipt_json.get("package_hash") != bundle.content_hash
        ):
            publication.graph_projection_status = "pending_types"
            publication.graph_projection_receipt_json = {**base_receipt, "reason": "current_schema_validation_required"}
            return None
        if confirmation and confirmation.get("activation_id") != str(activation.id):
            raise CompanyAnalysisGate("company_projection_confirmed_schema_changed")
        candidate = self.candidate(extraction=extraction, publication=publication, mapping=mapping)
        known = (
            {item.type_ref for item in bundle.schema.object_types},
            {item.property_ref for item in bundle.schema.property_types},
            {item.link_type_ref for item in bundle.schema.link_types},
        )
        unknown = {
            "entity_types": sorted({item.object_type_ref for item in candidate.objects} - known[0]),
            "property_refs": sorted(
                (
                    {key for item in candidate.objects for key in item.properties}
                    | {item.predicate_ref for item in candidate.assertions}
                )
                - known[1]
            ),
            "link_types": sorted({item.link_type_ref for item in candidate.links} - known[2]),
        }
        if any(unknown.values()):
            publication.graph_projection_status = "pending_types"
            publication.graph_projection_receipt_json = {
                **base_receipt,
                "unknown_types": unknown,
                "activation_id": str(activation.id),
            }
            return None
        try:
            validation = await self.ontology._engine.validate_candidate(package=bundle, candidate=candidate)
        except OntologyCandidateRejected:
            publication.graph_projection_status = "held_semantic_review"
            publication.graph_projection_receipt_json = {**base_receipt, "reason": "unresolved_analysis_findings"}
            return None
        if not validation["passed"]:
            publication.graph_projection_status = "held_validation"
            publication.graph_projection_receipt_json = {**base_receipt, "validation": validation}
            return None
        # A document confers no authority to install unrelated rules or actions.
        object_refs = {item.object_type_ref for item in candidate.objects}
        property_refs = {key for item in candidate.objects for key in item.properties} | {
            item.predicate_ref for item in candidate.assertions
        }
        link_refs = {item.link_type_ref for item in candidate.links}
        schema = OntologySchemaBundle(
            object_types=[item for item in bundle.schema.object_types if item.type_ref in object_refs],
            property_types=[item for item in bundle.schema.property_types if item.property_ref in property_refs],
            link_types=[item for item in bundle.schema.link_types if item.link_type_ref in link_refs],
        )
        scoped_bundle = replace(bundle, schema=schema, rules=(), actions=(), queries=())
        run_key = f"publication-projection:{publication.id}:{extraction.candidate_hash}:{analysis_hash(mapping)}"
        run = await db.scalar(
            select(CompanyOntologyCurationRun).where(
                CompanyOntologyCurationRun.tenant_id == publication.tenant_id,
                CompanyOntologyCurationRun.idempotency_key == run_key,
            )
        )
        if run is None:
            run = CompanyOntologyCurationRun(
                tenant_id=publication.tenant_id,
                activation_id=activation.id,
                idempotency_key=run_key,
                source_contract_versions_json=[],
                evidence_scope_json={"source_refs": extraction.model_receipt_json["source_refs"]},
                authority_snapshot_json=base_receipt,
                requested_operations_json=["project_publication"],
                model_prompt_receipts_json=[extraction.model_receipt_json],
                candidate_patch_json=candidate.model_dump(mode="json"),
                candidate_patch_hash=analysis_hash(candidate.model_dump(mode="json")),
                coverage_ledger_json=candidate.coverage_ledger.model_dump(),
                conflict_ledger_json=candidate.conflict_ledger.model_dump(),
                unresolved_questions_json=[],
                acceptance_result_json=validation,
                status="completed",
                created_by_type=principal.actor_type,
                created_by_id=principal.actor_id,
                accountable_user_id=principal.accountable_user_id,
            )
            db.add(run)
            await db.flush()
        existing = await db.scalar(
            select(CompanyOntologyRelease).where(
                CompanyOntologyRelease.tenant_id == publication.tenant_id,
                CompanyOntologyRelease.publication_id == publication.id,
                CompanyOntologyRelease.status == "active",
                CompanyOntologyRelease.curation_run_id == run.id,
            )
        )
        if existing:
            return existing
        release = await self.ontology._materialize_release(
            db,
            principal=principal,
            activation=activation,
            package_version=version,
            bundle=scoped_bundle,
            run=run,
            proposal=proposal,
            candidate=candidate,
            review_evaluation={"approved": True, "review_set_hash": publication.review_set_hash},
            review_receipts=[base_receipt],
            valid_from=publication.valid_from,
            valid_until=publication.valid_until,
            trace_id=trace_id,
            policy={"authority": "publication", **base_receipt},
            restored_from_release_id=None,
            publication_id=publication.id,
        )
        publication.graph_projection_status = "active"
        publication.graph_projection_receipt_json = {
            **base_receipt,
            "release_id": str(release.id),
            "activation_id": str(activation.id),
        }
        publication.companion_release_ref = f"company-ontology-release://{release.id}"
        return release

    async def analysis_for_publication(self, db, publication):
        job = await db.scalar(
            select(CompanyKnowledgeAnalysisJob)
            .where(
                CompanyKnowledgeAnalysisJob.tenant_id == publication.tenant_id,
                CompanyKnowledgeAnalysisJob.document_id == publication.document_id,
                CompanyKnowledgeAnalysisJob.namespace == publication.namespace,
                CompanyKnowledgeAnalysisJob.input_hash == publication.content_hash,
                CompanyKnowledgeAnalysisJob.request_json["source_refs"]
                == sorted(set(publication.source_refs_json or [])),
            )
            .order_by(CompanyKnowledgeAnalysisJob.created_at.desc(), CompanyKnowledgeAnalysisJob.id)
            .limit(1)
        )
        extraction = (
            await db.scalar(
                select(CompanyKnowledgeExtraction).where(
                    CompanyKnowledgeExtraction.job_id == job.id, CompanyKnowledgeExtraction.validated.is_(True)
                )
            )
            if job
            else None
        )
        return job, extraction

    async def view(self, db, *, principal, publication):
        from app.services.company_knowledge_analysis import analysis_job_view
        from app.services.company_knowledge_gateway import CompanyKnowledgeGateway, CompanyKnowledgeReadRequest

        readable = await CompanyKnowledgeGateway().read(
            db,
            principal=principal,
            request=CompanyKnowledgeReadRequest(
                document_id=None,
                publication_id=publication.id,
                segment_ids=(),
                max_chars=1,
                trace_id=f"publication:{publication.id}:projection-view:{uuid.uuid4()}",
            ),
        )
        if readable.status != "ok":
            raise PermissionError("company_publication_projection_read_denied")
        job, extraction = await self.analysis_for_publication(db, publication)
        view = {
            "publication_id": str(publication.id),
            "content_hash": publication.content_hash,
            "status": publication.graph_projection_status,
            "job": analysis_job_view(job) if job else None,
            "candidate": None,
            "candidate_hash": None,
            "receipt": None,
            "schemas": [],
            "can_reanalyze": False,
            "can_manage": principal.actor_type == "user"
            and principal.accountable_role in {"org_admin", "platform_admin"},
        }
        if extraction:
            try:
                await authorize_extraction_context(db, principal=principal, extraction=extraction)
            except PermissionError:
                view["context_access"] = "denied"
                view["can_reanalyze"] = view["can_manage"]
                return view
            view.update(
                candidate=extraction.candidate_json,
                candidate_hash=extraction.candidate_hash,
                receipt=publication.graph_projection_receipt_json,
            )
        if principal.actor_type == "user" and principal.accountable_role in {"org_admin", "platform_admin"}:
            activations = (
                await db.scalars(
                    select(CompanyOntologyActivation).where(
                        CompanyOntologyActivation.tenant_id == publication.tenant_id,
                        CompanyOntologyActivation.status == "active",
                    )
                )
            ).all()
            for activation in activations:
                _installation, _version, bundle = await self.ontology._installation_bundle(
                    db, tenant_id=publication.tenant_id, installation_id=activation.installation_id
                )
                view["schemas"].append(
                    {
                        "activation_id": str(activation.id),
                        "namespace": activation.namespace,
                        "entity_types": [item.type_ref for item in bundle.schema.object_types],
                        "property_refs": [item.property_ref for item in bundle.schema.property_types],
                        "link_types": [item.link_type_ref for item in bundle.schema.link_types],
                    }
                )
        return view

    async def backfill(self, db, *, principal, publication, trace_id):
        from app.models.company_knowledge import CompanyKnowledgeEvidence, CompanyKnowledgeEvent
        from app.models.knowledge import KnowledgeDocument
        from app.services.company_knowledge_service import CompanyKnowledgeService, _event_input
        from app.services.company_knowledge_analysis import enqueue_evidence_analysis, analysis_job_view

        if principal.actor_type != "user" or principal.accountable_role not in {"org_admin", "platform_admin"}:
            raise PermissionError("company_projection_backfill_requires_administrator")
        service = CompanyKnowledgeService(data_root=self.analyzer.data_root)
        policy = await service._require_permission(
            db, principal=principal, resource=await service._publication_resource(db, publication), action="publish"
        )
        if publication.status != "active":
            raise ValueError("active_company_knowledge_publication_required")
        command_key = f"publication:{publication.id}:analysis-command:{analysis_hash(trace_id)}"
        previous = await db.scalar(
            select(CompanyKnowledgeEvent).where(
                CompanyKnowledgeEvent.tenant_id == principal.tenant_id,
                CompanyKnowledgeEvent.idempotency_key == command_key,
            )
        )
        if previous:
            if previous.accountable_user_id != principal.accountable_user_id or previous.actor_id != principal.actor_id:
                raise ValueError("company_analysis_backfill_idempotency_conflict")
            prior_job = await db.get(CompanyKnowledgeAnalysisJob, uuid.UUID(previous.payload_json["job_id"]))
            return analysis_job_view(prior_job)

        async def record_command(job):
            await append_company_knowledge_event(
                db,
                event_input=_event_input(
                    principal=principal,
                    event_type="company_knowledge.publication_analysis_requested",
                    resource_type="publication",
                    resource_id=publication.id,
                    resource_version=publication.version,
                    source_refs=tuple(publication.source_refs_json or []),
                    source_hash=job.request_hash,
                    policy_snapshot=policy,
                    trace_id=trace_id,
                    idempotency_key=command_key,
                    outcome=job.status,
                    payload={"job_id": str(job.id), "requires_projection_confirmation": True},
                ),
            )
            return analysis_job_view(job)

        job, old_extraction = await self.analysis_for_publication(db, publication)
        reanalyze = False
        if job:
            if analysis_job_view(job)["can_retry"]:
                await self.analyzer.queue_retry(
                    db,
                    principal=principal,
                    job=job,
                    policy=policy,
                    source_refs=tuple(publication.source_refs_json or []),
                    trace_id=trace_id,
                )
                return await record_command(job)
            if job.status == "completed" and old_extraction:
                try:
                    await authorize_extraction_context(db, principal=principal, extraction=old_extraction)
                except PermissionError:
                    reanalyze = True
            if not reanalyze:
                return await record_command(job)
        refs = list(publication.source_refs_json or [])
        if not refs:
            raise CompanyAnalysisGate("company_analysis_source_unavailable")
        evidence = await db.get(
            CompanyKnowledgeEvidence, uuid.UUID(refs[0].removeprefix("company-evidence://").split("#", 1)[0])
        )
        document = await db.get(KnowledgeDocument, publication.document_id)
        if evidence is None or document is None or document.canonical_md_sha256 != publication.content_hash:
            raise CompanyAnalysisGate("company_analysis_source_changed")
        job = await enqueue_evidence_analysis(
            db,
            evidence=evidence,
            document=document,
            principal=principal,
            namespace=publication.namespace,
            source_refs=refs,
            generation=f"publication:{publication.id}:{trace_id}" if reanalyze else "initial",
        )
        publication.graph_projection_status = "analysis_queued"
        return await record_command(job)

    async def confirm_types(
        self, db, *, principal, publication, candidate_hash, content_hash, activation_id, mapping, reason, trace_id
    ):
        from app.services.company_knowledge_service import CompanyKnowledgeService, _event_input

        if principal.actor_type != "user" or principal.accountable_role not in {"org_admin", "platform_admin"}:
            raise PermissionError("company_projection_schema_confirmation_requires_administrator")
        if publication.status != "active" or content_hash != publication.content_hash or not reason.strip():
            raise ValueError("company_projection_confirmation_subject_invalid")
        service = CompanyKnowledgeService(data_root=self.analyzer.data_root)
        policy = await service._require_permission(
            db, principal=principal, resource=await service._publication_resource(db, publication), action="publish"
        )
        _job, extraction = await self.analysis_for_publication(db, publication)
        if extraction is None or extraction.candidate_hash != candidate_hash:
            raise CompanyAnalysisGate("company_projection_candidate_changed")
        publication.analysis_extraction_id = extraction.id
        receipt = {
            "user_id": str(principal.accountable_user_id),
            "reason": reason.strip(),
            "mapping": mapping,
            "candidate_hash": candidate_hash,
            "content_hash": content_hash,
            "publication_id": str(publication.id),
            "activation_id": str(activation_id),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "permission_decision": policy,
        }
        await self.apply(db, principal=principal, publication=publication, confirmation=receipt, trace_id=trace_id)
        await append_company_knowledge_event(
            db,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.projection_types_confirmed",
                resource_type="publication",
                resource_id=publication.id,
                resource_version=publication.version,
                source_refs=tuple(publication.source_refs_json or []),
                source_hash=analysis_hash(receipt),
                policy_snapshot=policy,
                trace_id=trace_id,
                idempotency_key=f"publication:{publication.id}:types:{analysis_hash(receipt)}",
                outcome=publication.graph_projection_status,
                payload={"confirmation": receipt},
            ),
        )
        return publication


async def derived_publication_permission(db, *, principal, release, action):
    from app.models.company_knowledge import (
        CompanyKnowledgePublication,
        CompanyKnowledgeEvidence,
        CompanyKnowledgeSourceContract,
    )
    from app.services.company_knowledge_gateway import CompanyKnowledgeGateway, CompanyKnowledgeReadRequest
    from app.services.company_knowledge_permissions import CompanyKnowledgePermissionDecision

    denial = "publication_not_active"
    publication = await db.scalar(
        select(CompanyKnowledgePublication).where(
            CompanyKnowledgePublication.id == release.publication_id,
            CompanyKnowledgePublication.tenant_id == principal.tenant_id,
        )
    )
    allowed = False
    if (
        publication
        and publication.graph_projection_status == "active"
        and release.status == "active"
        and action in {"query", "read", "cite", "search", "discover"}
    ):
        extraction = (
            await db.get(CompanyKnowledgeExtraction, publication.analysis_extraction_id)
            if publication.analysis_extraction_id
            else None
        )
        if extraction and extraction.validated and extraction.input_hash == publication.content_hash:
            result = await CompanyKnowledgeGateway().read(
                db,
                principal=principal,
                request=CompanyKnowledgeReadRequest(
                    document_id=None,
                    publication_id=publication.id,
                    segment_ids=(),
                    max_chars=1,
                    trace_id=f"publication-projection:{release.id}:authority:{uuid.uuid4()}",
                ),
            )
            if result.status == "ok":
                try:
                    await authorize_extraction_context(db, principal=principal, extraction=extraction)
                    allowed = True
                    for ref in extraction.model_receipt_json["source_refs"]:
                        evidence = await db.get(
                            CompanyKnowledgeEvidence,
                            uuid.UUID(ref.removeprefix("company-evidence://").split("#", 1)[0]),
                        )
                        contract = (
                            await db.get(CompanyKnowledgeSourceContract, evidence.source_contract_id)
                            if evidence
                            else None
                        )
                        if (
                            not evidence
                            or evidence.status != "accepted"
                            or not contract
                            or contract.status != "active"
                            or contract.version != evidence.source_contract_version
                        ):
                            allowed, denial = False, "source_contract_changed"
                            break
                except PermissionError:
                    denial = "company_analysis_context_read_denied"
            else:
                denial = "publication_read_denied"
    return CompanyKnowledgePermissionDecision(
        allowed=allowed,
        requested_action=action,
        allowed_actions=("query", "read", "cite") if allowed else (),
        authority_sources=("derived_publication",),
        sensitivity_ceiling=publication.sensitivity if publication else None,
        source_acl_snapshot_hash=publication.content_hash if publication else None,
        redaction_policy="none",
        deny_reason_code=None if allowed else denial,
        audit_payload={"publication_id": str(publication.id) if publication else None, "release_id": str(release.id)},
    )
