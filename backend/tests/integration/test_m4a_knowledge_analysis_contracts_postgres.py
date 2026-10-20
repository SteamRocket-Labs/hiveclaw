"""Three real intake paths retain model findings; publication failures roll back the graph."""

from datetime import datetime, timezone
import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.models.company_knowledge import CompanyKnowledgeProposal, CompanyKnowledgeEvent
from app.models.company_knowledge_analysis import CompanyKnowledgeExtraction
from app.models.knowledge import KnowledgeDocument, KnowledgeSegment
from app.services.company_knowledge_analysis import (
    CompanyKnowledgeAnalyzer,
    CompanyKnowledgeAnalysisReview,
    proposal_analysis_job,
)
from app.services.company_knowledge_promotion import (
    CompanyKnowledgePromotionService,
    PersonalPromotionIntakeRequest,
    LegacyPromotionIntakeRequest,
)
from app.services.company_knowledge_service import CompanyKnowledgeMaterializationRequest
from tests.integration import test_m4a_knowledge_projection_postgres as flow

prepared_analysis = flow.prepared_analysis
app_user_engine = flow.app_user_engine
app_user_sessionmaker = flow.app_user_sessionmaker
migrated_pg_url = flow.migrated_pg_url
owner_engine = flow.owner_engine
owner_sessionmaker = flow.owner_sessionmaker
pg_container = flow.pg_container

CONTRADICTORY_SOURCE = "# Approval policy\n\nApproval is required.\n\nApproval is not required.\n"


@pytest.mark.parametrize(
    "prepared_analysis,entry",
    [
        ({"markdown": CONTRADICTORY_SOURCE, "title": "Approval policy"}, "direct_ingest"),
        ({"markdown": CONTRADICTORY_SOURCE, "title": "Approval policy"}, "personal_promotion"),
        ({"markdown": CONTRADICTORY_SOURCE, "title": "Approval policy"}, "legacy_import"),
    ],
    indirect=["prepared_analysis"],
)
async def test_each_intake_path_preserves_nonempty_model_conflicts_for_real_review(
    prepared_analysis,
    entry,
    app_user_sessionmaker,
    monkeypatch,
):
    tenant_id, user_id, _analysis_id, imported, service, principal, root = prepared_analysis
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    promotion = CompanyKnowledgePromotionService(data_root=root, company_service=service)
    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        if entry == "direct_ingest":
            proposal = await flow.submitted_proposal(db, prepared_analysis)
        elif entry == "personal_promotion":
            digest = hashlib.sha256(CONTRADICTORY_SOURCE.encode()).hexdigest()
            relative = f"persons/{user_id}/kb/documents/{digest[:2]}/{digest}.md"
            artifact = root / relative
            artifact.parent.mkdir(parents=True)
            artifact.write_text(CONTRADICTORY_SOURCE)
            personal_id = uuid4()
            db.add(
                KnowledgeDocument(
                    id=personal_id,
                    tenant_id=tenant_id,
                    scope_type="person",
                    scope_id=user_id,
                    owner_user_id=user_id,
                    source_kind="owner_note",
                    source_uri="owner-note://approval-policy",
                    source_sha256=digest,
                    artifact_hash=digest,
                    title="Approval policy",
                    status="ready",
                    sensitivity="PL2_pii",
                    agent_searchable=True,
                    canonical_md_path=relative,
                    canonical_md_sha256=digest,
                    doc_metadata_json={},
                    created_by_user_id=user_id,
                )
            )
            await db.flush()
            db.add(
                KnowledgeSegment(
                    tenant_id=tenant_id,
                    document_id=personal_id,
                    scope_type="person",
                    scope_id=user_id,
                    position=0,
                    segment_hash=digest,
                    heading_path_json=["Approval policy"],
                    content=CONTRADICTORY_SOURCE,
                    token_count=20,
                    segment_metadata_json={},
                )
            )
            await db.flush()
            imported = await promotion.queue_personal_promotion(
                db,
                principal=principal,
                request=PersonalPromotionIntakeRequest(
                    document_id=personal_id,
                    proposed_namespace="company/policies",
                    purpose="Owner-approved review of the full policy",
                    risk_level="normal",
                    title=None,
                    attest_scope_change=True,
                    idempotency_key="conflict-personal",
                    trace_id="conflict-personal",
                ),
            )
            proposal = None
        else:
            company_dir = root / f"enterprise_info_{tenant_id}"
            artifact = company_dir / "knowledge_base/approval.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(CONTRADICTORY_SOURCE)
            imported = await promotion.queue_legacy_promotion(
                db,
                principal=principal,
                company_dir=company_dir,
                request=LegacyPromotionIntakeRequest(
                    relative_path="knowledge_base/approval.md",
                    expected_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    proposed_namespace="company/policies",
                    proposed_sensitivity="PL2_pii",
                    purpose="Review the exact legacy policy",
                    risk_level="normal",
                    title="Approval policy",
                    attest_scope_change=True,
                    idempotency_key="conflict-legacy",
                    trace_id="conflict-legacy",
                ),
            )
            proposal = None
        proposal_id = proposal.id if proposal else None
        await db.commit()
    if entry != "direct_ingest":
        imported = await service.process_import_job(
            tenant_id=tenant_id, job_id=imported.id, session_factory=app_user_sessionmaker
        )
        assert imported.status == "completed" and imported.proposal_id
        proposal_id = imported.proposal_id
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            proposal = await db.get(CompanyKnowledgeProposal, proposal_id)
            candidate = await promotion.get_candidate(db, principal=principal, job_id=imported.id)
            assert candidate["markdown"] == CONTRADICTORY_SOURCE
            await service.materialize_proposal(
                db,
                principal=principal,
                proposal_id=proposal_id,
                expected_state_version=proposal.state_version,
                trace_id="conflict-materialization",
                request=CompanyKnowledgeMaterializationRequest(
                    title=candidate["title"],
                    markdown=candidate["markdown"],
                    expected_proposed_content_hash=proposal.proposed_content_hash,
                    attest_candidate_applied=True,
                    idempotency_key="conflict-materialization",
                ),
            )
            await db.commit()

    def finding(candidate):
        return {
            **candidate,
            "unresolved_conflicts": [
                {
                    "summary": "Conflicting approval requirements",
                    "reason": "The supplied policy both requires and does not require approval.",
                    "source_refs": candidate["covered_source_refs"],
                    "publication_ids": [],
                }
            ],
        }

    with flow.model_server(transform=finding) as (origin, requests, _):
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            await flow.configure_model(db, tenant_id=tenant_id, origin=origin)
            proposal = await db.get(CompanyKnowledgeProposal, proposal_id)
            job = await proposal_analysis_job(db, proposal)
            assert job and job.status == "queued"
            job_id = job.id
        result = await CompanyKnowledgeAnalyzer(data_root=root).process(
            tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker
        )
        assert result["status"] == "completed", result
        assert len(requests) == 1
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            extraction = await db.scalar(
                select(CompanyKnowledgeExtraction).where(CompanyKnowledgeExtraction.job_id == job_id)
            )
            assert extraction.validated and extraction.candidate_json["unresolved_conflicts"]
            assert extraction.model_receipt_json["input_snapshot"]["source"]["content"] == CONTRADICTORY_SOURCE
            proposal = await service.get_proposal_for_review(db, principal=principal, proposal_id=proposal_id)
            assert proposal.conflict_candidates_json == extraction.candidate_json["unresolved_conflicts"]
            view = await CompanyKnowledgeAnalysisReview(data_root=root).view(db, principal=principal, proposal=proposal)
            assert view["candidate"]["unresolved_conflicts"] == proposal.conflict_candidates_json
        for scope, expected in ((str(tenant_id), 1), (str(uuid4()), 0), ("", 0)):
            async with app_user_sessionmaker() as db:
                assert not await db.scalar(text("SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user"))
                await db.execute(text("SELECT set_config('app.current_tenant_id', :scope, true)"), {"scope": scope})
                assert (
                    await db.scalar(
                        text("SELECT count(*) FROM company_knowledge_extractions WHERE job_id=:job"), {"job": job_id}
                    )
                    == expected
                )
                assert (
                    await db.scalar(
                        text("SELECT count(*) FROM company_knowledge_analysis_jobs WHERE id=:job"), {"job": job_id}
                    )
                    == expected
                )


@pytest.mark.parametrize(
    "prepared_analysis", [{"namespace": "project", "markdown": "# Owner\n\nOwner is Ada.\n"}], indirect=True
)
async def test_publication_projection_failure_rolls_back_all_rows_and_preserves_the_approved_retry(
    prepared_analysis,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.services.company_knowledge_service import CompanyKnowledgeReviewRequest
    from app.services.company_ontology_engine import ReferenceOntologyEngine, OntologyEngineUnavailable
    from app.services.company_ontology_service import (
        CompanyOntologyService,
        OntologyPackageInstallRequest,
        OntologyActivationRequest,
    )

    tenant_id, _user_id, job_id, _imported, service, principal, root = prepared_analysis
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        ontology = CompanyOntologyService(knowledge_service=service)
        installation = await ontology.install_package(
            db,
            principal=principal,
            request=OntologyPackageInstallRequest(
                package_key="project-goal-deliverable-owner",
                version="1.0.0",
                idempotency_key="atomic-install",
                trace_id="atomic-install",
            ),
        )
        activation = await ontology.create_activation(
            db,
            principal=principal,
            request=OntologyActivationRequest(
                installation_id=installation.id,
                namespace="project",
                configuration={},
                idempotency_key="atomic-activation",
                trace_id="atomic-activation",
            ),
        )
        await ontology.dry_run_activation(
            db,
            principal=principal,
            activation_id=activation.id,
            idempotency_key="atomic-dry-run",
            trace_id="atomic-dry-run",
        )
        proposal = await flow.submitted_proposal(db, prepared_analysis)
        proposal_id = proposal.id
        await db.commit()

    def known_owner(candidate):
        return {
            **candidate,
            "entities": [
                {
                    **candidate["entities"][0],
                    "type_ref": "project.Owner",
                    "display_name": "Ada",
                    "properties": {"project.name": "Ada"},
                }
            ],
        }

    with flow.model_server(transform=known_owner) as (origin, requests, _):
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            await flow.configure_model(db, tenant_id=tenant_id, origin=origin)
        result = await CompanyKnowledgeAnalyzer(data_root=root).process(
            tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker
        )
        assert result["status"] == "completed", result
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            proposal = await db.get(CompanyKnowledgeProposal, proposal_id)
            approved = await service.record_review(
                db,
                principal=principal,
                proposal_id=proposal_id,
                expected_state_version=proposal.state_version,
                trace_id="atomic-review",
                request=CompanyKnowledgeReviewRequest(
                    decision="approve",
                    reviewer_role="org_admin",
                    reason="Reviewed owner evidence",
                    evidence_refs=tuple(proposal.source_refs_json),
                    policy_snapshot={},
                ),
            )
            approved_version = approved.state_version
            await db.commit()

        async def unavailable(self, *args, **kwargs):
            raise RuntimeError("Controlled projection provider outage after staging graph rows")

        with monkeypatch.context() as failure:
            failure.setattr(ReferenceOntologyEngine, "materialize_release_projection", unavailable)
            async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
                with pytest.raises(OntologyEngineUnavailable):
                    await service.publish_proposal(
                        db,
                        principal=principal,
                        proposal_id=proposal_id,
                        expected_state_version=approved_version,
                        valid_from=datetime.now(timezone.utc),
                        valid_until=None,
                        trace_id="atomic-failed-publish",
                    )
                await db.rollback()
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            for table in (
                "company_knowledge_publications",
                "company_ontology_releases",
                "company_ontology_objects",
                "company_ontology_assertions",
                "company_ontology_links",
                "company_ontology_evidence_bindings",
                "company_ontology_curation_runs",
            ):
                assert await db.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM company_knowledge_events WHERE event_type='company_knowledge.published'")
                )
                == 0
            )
            assert (await db.get(CompanyKnowledgeProposal, proposal_id)).status == "approved"
            retried = await service.publish_proposal(
                db,
                principal=principal,
                proposal_id=proposal_id,
                expected_state_version=approved_version,
                valid_from=datetime.now(timezone.utc),
                valid_until=None,
                trace_id="atomic-successful-publish",
            )
            assert retried.graph_projection_status == "active"
            await db.commit()
            assert await db.scalar(text("SELECT count(*) FROM company_knowledge_publications")) == 1
            assert await db.scalar(text("SELECT count(*) FROM company_ontology_objects")) == 1
            assert len(requests) == 1
            from app.services.company_knowledge_evidence import verify_company_knowledge_event_chain

            events = (
                await db.scalars(select(CompanyKnowledgeEvent).order_by(CompanyKnowledgeEvent.stream_sequence))
            ).all()
            assert verify_company_knowledge_event_chain(events)["valid"], (
                "Analysis and graph publication broke the evidence chain"
            )
