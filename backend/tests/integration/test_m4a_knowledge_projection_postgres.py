"""Knowledge analysis, original text and graph projections share publication authority."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest
from uuid import uuid4

from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.models.tenant import Tenant
from app.models.user import User
from app.services.company_knowledge_service import CompanyEvidenceIngestRequest, CompanyKnowledgeService
from tests.integration import conftest as persistence
from tests.integration.test_company_knowledge_closed_loop import _contract, _principal

app_user_engine = persistence.app_user_engine
app_user_sessionmaker = persistence.app_user_sessionmaker
migrated_pg_url = persistence.migrated_pg_url
owner_engine = persistence.owner_engine
owner_sessionmaker = persistence.owner_sessionmaker
pg_container = persistence.pg_container


@pytest.fixture
async def prepared_analysis(owner_sessionmaker, app_user_sessionmaker, tmp_path, request):
    options = getattr(request, "param", {})
    namespace = options.get("namespace", "company/policies")
    tenant_id, user_id = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Knowledge projection", slug="projection-" + tenant_id.hex))
        await db.flush()
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                role="org_admin",
                username="projection-" + user_id.hex,
                email=f"{user_id}@projection.test",
                display_name="Knowledge administrator",
                password_hash="synthetic",
            )
        )
        await db.commit()
    service = CompanyKnowledgeService(data_root=tmp_path)
    principal = _principal(tenant_id=tenant_id, user_id=user_id)
    now = datetime.now(timezone.utc)
    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        contract = await service.register_source_contract(
            db,
            principal=principal,
            contract_input=replace(_contract(), allowed_namespaces=(namespace,)),
            idempotency_key="projection-source",
            trace_id="projection-source",
        )
        queued = await service.queue_evidence_import(
            db,
            principal=principal,
            request=CompanyEvidenceIngestRequest(
                source_contract_id=contract.id,
                source_contract_version=contract.version,
                evidence_kind="document",
                source_item_id="employee-handbook",
                source_revision="one",
                title=options.get("title", "Projection source"),
                markdown=options.get(
                    "markdown", "# Launch\n\nThe new instrument is a type not installed in the ontology.\n"
                ),
                typed_payload=None,
                external_artifact_ref=None,
                schema_ref="schema://company-document/v1",
                source_acl_snapshot={"role_names": ["org_admin", "member"]},
                proposed_namespace=namespace,
                proposed_sensitivity="PL2_pii",
                occurred_at=None,
                effective_from=now,
                effective_until=None,
                observed_at=now,
                cursor={},
                sequence=None,
                coverage_ledger={"complete": True, "total_units": 1, "covered_units": 1, "missing_units": []},
                purpose="Publish source-backed knowledge",
                idempotency_key="projection-import",
                trace_id="projection-import",
            ),
        )
        await db.commit()
    completed = await service.process_import_job(
        tenant_id=tenant_id, job_id=queued.id, session_factory=app_user_sessionmaker
    )
    assert completed.status == "completed" and completed.evidence_id is not None
    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        assert await db.scalar(text("SELECT to_regclass('company_knowledge_analysis_jobs') IS NOT NULL")), (
            "Accepted canonical evidence has no durable analysis consumer"
        )
        rows = (
            await db.execute(text("SELECT evidence_id,status,accountable_user_id FROM company_knowledge_analysis_jobs"))
        ).all()
        assert rows == [(completed.evidence_id, "queued", user_id)]
        analysis_id = await db.scalar(text("SELECT id FROM company_knowledge_analysis_jobs"))
    return tenant_id, user_id, analysis_id, completed, service, principal, tmp_path


async def test_accepted_evidence_enqueues_one_lossless_analysis_in_the_ingestion_transaction(
    prepared_analysis,
    app_user_sessionmaker,
):
    tenant_id, _user_id, analysis_id, _completed, _service, _principal, tmp_path = prepared_analysis
    from app.services.company_knowledge_analysis import CompanyKnowledgeAnalyzer

    held = await CompanyKnowledgeAnalyzer(data_root=tmp_path).process(
        tenant_id=tenant_id, job_id=analysis_id, session_factory=app_user_sessionmaker
    )
    assert held["status"] == "held" and held["error_code"] == "company_analysis_configured_model_unavailable", held
    assert held["attempt"] == 0 and held["can_retry"]
    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        assert await db.scalar(text("SELECT count(*) FROM company_knowledge_extractions")) == 0


@contextmanager
def model_server(*, status=200, transform=None, finish_reason="stop"):
    requests = []
    long_value = "Source-backed candidate detail. " * 1000

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            packet = json.loads(request["messages"][-1]["content"])
            candidate = {
                "schema_version": "hive.company_evidence_analysis.v1",
                "entities": [
                    {
                        "key": "instrument",
                        "type_ref": "not_installed.Instrument",
                        "display_name": "New instrument",
                        "properties": {"notes": long_value},
                        "source_refs": packet["source_refs"],
                    }
                ],
                "assertions": [],
                "links": [],
                "covered_source_refs": packet["source_refs"],
                "unresolved_conflicts": [],
                "resolved_conflicts": [],
                "duplicate_candidates": [],
                "unresolved_questions": [],
            }
            if transform:
                candidate = transform(candidate)
            response = {
                "id": "reference-company-analysis",
                "object": "chat.completion",
                "created": 1,
                "model": request["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish_reason,
                        "message": {"role": "assistant", "content": json.dumps(candidate)},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 6000, "total_tokens": 6100},
            }
            body = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests, long_value
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_configured_model_receipt_and_unknown_type_candidate_preserve_the_complete_semantic_payload(
    prepared_analysis,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.models.llm import LLMModel
    from app.models.tenant_setting import TenantSetting
    from app.models.company_knowledge_analysis import CompanyKnowledgeExtraction
    from app.services.company_knowledge_analysis import CompanyKnowledgeAnalyzer
    from app.services.secrets_provider import get_secrets_provider

    tenant_id, _user_id, analysis_id, _completed, _service, _principal, tmp_path = prepared_analysis
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    model_id = uuid4()
    with model_server() as (origin, requests, long_value):
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            db.add(
                LLMModel(
                    id=model_id,
                    tenant_id=tenant_id,
                    provider="openai",
                    model="analysis-reference-v1",
                    label="Reference analysis model",
                    base_url=origin,
                    api_key_encrypted=get_secrets_provider().encrypt("synthetic-company-model-key"),
                    enabled=True,
                    max_input_tokens=32768,
                    max_output_tokens=8096,
                    reasoning_mode="enabled",
                    reasoning_effort="high",
                    temperature=0,
                    provider_options={},
                )
            )
            db.add(TenantSetting(tenant_id=tenant_id, key="default_model_id", value={"model_id": str(model_id)}))
            await db.commit()
        result = await CompanyKnowledgeAnalyzer(data_root=tmp_path).process(
            tenant_id=tenant_id, job_id=analysis_id, session_factory=app_user_sessionmaker
        )
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            dispatch = await db.scalar(
                text("SELECT dispatch_receipt_json FROM company_knowledge_analysis_jobs WHERE id=:id"),
                {"id": analysis_id},
            )
        assert result["status"] == "completed", {"result": result, "dispatch": dispatch, "http_requests": len(requests)}
        assert len(requests) == 1 and requests[0]["model"] == "analysis-reference-v1"
        assert requests[0]["max_completion_tokens"] == 8096 and requests[0]["reasoning_effort"] == "high"
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            from sqlalchemy import select

            extraction = await db.scalar(
                select(CompanyKnowledgeExtraction).where(
                    CompanyKnowledgeExtraction.job_id == analysis_id, CompanyKnowledgeExtraction.validated.is_(True)
                )
            )
            assert extraction.candidate_json["entities"][0]["type_ref"] == "not_installed.Instrument"
            assert extraction.candidate_json["entities"][0]["properties"]["notes"] == long_value
            assert extraction.model_receipt_json["model_id"] == str(model_id)
            assert "synthetic-company-model-key" not in json.dumps(extraction.model_receipt_json)
            assert await db.scalar(text("SELECT count(*) FROM company_ontology_objects")) == 0
            proposal = await submitted_proposal(db, prepared_analysis)
            from app.services.company_knowledge_service import CompanyKnowledgeReviewRequest

            proposal = await _service.record_review(
                db,
                principal=_principal,
                proposal_id=proposal.id,
                expected_state_version=proposal.state_version,
                trace_id="projection-analyzed-review",
                request=CompanyKnowledgeReviewRequest(
                    decision="approve",
                    reviewer_role="org_admin",
                    reason="Reviewed complete analysis and original source",
                    evidence_refs=tuple(proposal.source_refs_json),
                    policy_snapshot={},
                ),
            )
            publication = await _service.publish_proposal(
                db,
                principal=_principal,
                proposal_id=proposal.id,
                expected_state_version=proposal.state_version,
                valid_from=datetime.now(timezone.utc),
                valid_until=None,
                trace_id="projection-publish",
            )
            assert publication.graph_projection_status == "pending_types"
            assert publication.analysis_extraction_id == extraction.id
            publication_id = publication.id
            await db.commit()
        from app.services.company_knowledge_indexer import CompanyKnowledgeIndexer
        from app.services.company_knowledge_gateway import (
            CompanyKnowledgeGateway,
            CompanyKnowledgeReadRequest,
            CompanyKnowledgeSearchRequest,
        )

        indexed = await CompanyKnowledgeIndexer().process_pending(
            tenant_id=tenant_id, session_factory=app_user_sessionmaker, limit=20
        )
        assert indexed.completed >= 1 and indexed.failed == 0
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            gateway = CompanyKnowledgeGateway()
            read = await gateway.read(
                db,
                principal=_principal,
                request=CompanyKnowledgeReadRequest(
                    document_id=None,
                    publication_id=publication_id,
                    segment_ids=(),
                    max_chars=None,
                    trace_id="projection-original-read",
                ),
            )
            assert read.status == "ok" and any("The new instrument" in item.content for item in read.segments)
            found = await gateway.search(
                db,
                principal=_principal,
                request=CompanyKnowledgeSearchRequest(
                    query="instrument",
                    filters={"namespaces": ["company/policies"]},
                    limit=10,
                    trace_id="projection-original-search",
                ),
            )
            assert any(item.publication_id == publication_id for item in found.results)
            from app.models.company_knowledge import CompanyKnowledgePublication, CompanyKnowledgeSource
            from app.models.security_audit import ResourcePermission
            from app.services.company_knowledge_projection import CompanyKnowledgeProjector
            from app.services.company_ontology_service import (
                CompanyOntologyService,
                OntologyPackageInstallRequest,
                OntologyActivationRequest,
            )
            from app.services.company_ontology_gateway import CompanyOntologyGateway, OntologyQueryRequest

            ontology = CompanyOntologyService(knowledge_service=_service)
            installed = await ontology.install_package(
                db,
                principal=_principal,
                request=OntologyPackageInstallRequest(
                    package_key="project-goal-deliverable-owner",
                    version="1.0.0",
                    idempotency_key="projection-install",
                    trace_id="projection-install",
                ),
            )
            activation = await ontology.create_activation(
                db,
                principal=_principal,
                request=OntologyActivationRequest(
                    installation_id=installed.id,
                    namespace="project",
                    configuration={},
                    idempotency_key="projection-activation",
                    trace_id="projection-activation",
                ),
            )
            activation = await ontology.dry_run_activation(
                db,
                principal=_principal,
                activation_id=activation.id,
                idempotency_key="projection-dry-run",
                trace_id="projection-dry-run",
            )
            assert activation.status == "active"
            publication = await db.get(CompanyKnowledgePublication, publication_id)
            projector = CompanyKnowledgeProjector(data_root=tmp_path)
            view = await projector.view(db, principal=_principal, publication=publication)
            assert view["candidate_hash"] == extraction.candidate_hash and view["schemas"]
            member_id = uuid4()
            db.add(
                User(
                    id=member_id,
                    tenant_id=tenant_id,
                    role="member",
                    username="projection-member-" + member_id.hex,
                    email=f"{member_id}@projection.test",
                    display_name="Document reader",
                    password_hash="synthetic",
                )
            )
            await db.flush()
            member = _principal.__class__(
                tenant_id=tenant_id,
                accountable_user_id=member_id,
                accountable_role="member",
                actor_type="user",
                actor_id=member_id,
                purpose="interactive_session",
            )
            mapping = {
                "entity_types": {"not_installed.Instrument": "project.Owner"},
                "property_refs": {"notes": "project.name"},
                "link_types": {},
            }
            with pytest.raises(PermissionError, match="requires_administrator"):
                await projector.confirm_types(
                    db,
                    principal=member,
                    publication=publication,
                    candidate_hash=extraction.candidate_hash,
                    content_hash=publication.content_hash,
                    activation_id=activation.id,
                    mapping=mapping,
                    reason="Unprivileged confirmation",
                    trace_id="projection-denied-types",
                )
            await projector.confirm_types(
                db,
                principal=_principal,
                publication=publication,
                candidate_hash=extraction.candidate_hash,
                content_hash=publication.content_hash,
                activation_id=activation.id,
                mapping=mapping,
                reason="Confirmed the candidate as an owner in the installed schema",
                trace_id="projection-confirm-types",
            )
            assert publication.graph_projection_status == "active"
            db.add(
                ResourcePermission(
                    tenant_id=tenant_id,
                    principal_type="user",
                    principal_id=member_id,
                    resource_type="company_knowledge_document",
                    resource_id=publication.document_id,
                    actions=["read", "cite"],
                    effect="allow",
                    sensitivity_ceiling="PL2_pii",
                    purposes=["interactive_session"],
                )
            )
            await db.commit()
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            graph = CompanyOntologyGateway()

            def query():
                return OntologyQueryRequest(
                    namespaces=("project",),
                    object_type_refs=(),
                    object_ids=(),
                    query_ref=None,
                    query_input={},
                    include_facts=True,
                    include_links=True,
                    limit=20,
                    trace_id="projection-derived-read:" + str(uuid4()),
                )

            objects = await graph.query(db, principal=member, request=query())
            assert objects.payload["result_count"] == 1, objects.as_dict()
            assert objects.payload["objects"][0]["properties"]["project.name"] == long_value
            assert await db.scalar(text("SELECT count(*) FROM company_ontology_action_types")) == 0
            assert await db.scalar(text("SELECT count(*) FROM company_ontology_object_types")) == 1
            await _service.retire_publication(
                db,
                principal=_principal,
                publication_id=publication_id,
                reason="Retire source",
                trace_id="projection-retire",
            )
            assert (await graph.query(db, principal=member, request=query())).payload["result_count"] == 0
            restored = await _service.restore_publication(
                db,
                principal=_principal,
                publication_id=publication_id,
                reason="Restore reviewed source",
                valid_from=datetime.now(timezone.utc),
                trace_id="projection-restore",
            )
            assert restored.graph_projection_status == "active" and restored.id != publication_id
            assert (await graph.query(db, principal=member, request=query())).payload["result_count"] == 1
            source = await db.get(CompanyKnowledgeSource, _completed.source_id)
            source.status = "retired"
            await db.flush()
            assert (await graph.query(db, principal=member, request=query())).payload["result_count"] == 0
            assert len(requests) == 1
            await db.commit()


async def submitted_proposal(db, prepared):
    from app.services.company_knowledge_service import CompanyKnowledgeProposalRequest

    _tenant, _user, _analysis, imported, service, principal, _root = prepared
    proposal = await service.create_proposal(
        db,
        principal=principal,
        request=CompanyKnowledgeProposalRequest(
            proposal_kind="knowledge",
            source_id=imported.source_id,
            source_document_id=imported.document_id,
            source_revision_ref="company-import://" + str(imported.id),
            baseline_publication_id=None,
            baseline_version=None,
            proposed_patch={"operation": "publish_document", "title": "Projection source"},
            proposed_namespace=imported.request_json["proposed_namespace"],
            proposed_sensitivity="PL2_pii",
            source_refs=(f"company-evidence://{imported.evidence_id}",),
            source_coverage={"complete": True, "total_units": 1, "covered_units": 1, "missing_units": []},
            conflict_candidates=(),
            ontology_mapping={},
            risk_level="normal",
            required_review_policy={"minimum_approvals": 1, "required_roles": ["org_admin"], "separation": False},
            idempotency_key=f"projection-proposal:{imported.id}",
            trace_id="projection-proposal",
        ),
    )
    return await service.submit_proposal(
        db,
        principal=principal,
        proposal_id=proposal.id,
        expected_state_version=proposal.state_version,
        trace_id="projection-submit",
    )


async def test_analysis_gate_and_explicit_human_override_are_bound_to_the_exact_review_subject(
    prepared_analysis,
    app_user_sessionmaker,
):
    from app.services.company_knowledge_analysis import CompanyAnalysisGate, CompanyKnowledgeAnalysisReview
    from app.services.company_knowledge_service import CompanyKnowledgeReviewRequest

    tenant_id, _user, _analysis, _imported, service, principal, root = prepared_analysis
    review = CompanyKnowledgeReviewRequest(
        decision="approve",
        reviewer_role="org_admin",
        reason="Reviewed complete source",
        evidence_refs=(f"company-evidence://{_imported.evidence_id}",),
        policy_snapshot={},
    )
    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        proposal = await submitted_proposal(db, prepared_analysis)
        proposal_id = proposal.id
        await db.commit()
    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        with pytest.raises(CompanyAnalysisGate, match="company_analysis_required_before_review"):
            await service.record_review(
                db,
                principal=principal,
                proposal_id=proposal_id,
                request=review,
                expected_state_version=2,
                trace_id="projection-held-review",
            )
        await db.rollback()
        assert await db.scalar(text("SELECT count(*) FROM company_knowledge_reviews")) == 0
        assert (
            await db.scalar(text("SELECT status FROM company_knowledge_proposals WHERE id=:id"), {"id": proposal_id})
            == "submitted"
        )
        proposal = await service._locked_proposal(
            db, tenant_id=tenant_id, proposal_id=proposal_id, expected_state_version=2
        )
        lane = CompanyKnowledgeAnalysisReview(data_root=root)
        with pytest.raises(ValueError, match="reason_and_human"):
            await lane.override(
                db, principal=principal, proposal=proposal, reason="  ", trace_id="projection-empty-override"
            )
        receipt = await lane.override(
            db,
            principal=principal,
            proposal=proposal,
            reason="Manually checked complete original; graph analysis is unavailable",
            trace_id="projection-override",
        )
        assert receipt["job_status"] == "queued" and receipt["subject_hash"] == lane.subject(proposal)
        proposal = await service.record_review(
            db,
            principal=principal,
            proposal_id=proposal.id,
            request=review,
            expected_state_version=proposal.state_version,
            trace_id="projection-manual-review",
        )
        assert proposal.status == "approved"
        await db.commit()
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM company_knowledge_events WHERE event_type="
                    "'company_knowledge.analysis_gate_overridden'"
                )
            )
            == 1
        )


async def configure_model(db, *, tenant_id, origin):
    from app.models.llm import LLMModel
    from app.models.tenant_setting import TenantSetting
    from app.services.secrets_provider import get_secrets_provider

    model = LLMModel(
        id=uuid4(),
        tenant_id=tenant_id,
        provider="openai",
        model="analysis-reference-v1",
        label="Reference analysis model",
        base_url=origin,
        api_key_encrypted=get_secrets_provider().encrypt("synthetic-company-model-key"),
        enabled=True,
        max_input_tokens=32768,
        max_output_tokens=8096,
        temperature=0,
        provider_options={},
    )
    db.add(model)
    db.add(TenantSetting(tenant_id=tenant_id, key="default_model_id", value={"model_id": str(model.id)}))
    await db.commit()
    return model.id


@pytest.mark.parametrize(
    "http_status,invalid_candidate,expected_error,safe_retry",
    [
        (502, False, "company_analysis_provider_execution_unknown", False),
        (400, False, "company_analysis_provider_rejected", True),
        (200, True, "company_analysis_candidate_invalid", True),
    ],
)
async def test_transport_outcomes_and_quarantined_replies_never_trigger_implicit_resends(
    prepared_analysis,
    app_user_sessionmaker,
    monkeypatch,
    http_status,
    invalid_candidate,
    expected_error,
    safe_retry,
):
    from app.models.company_knowledge_analysis import CompanyKnowledgeExtraction
    from app.services.company_knowledge_analysis import CompanyAnalysisGate, CompanyKnowledgeAnalyzer
    from sqlalchemy import select

    tenant_id, _user, job_id, _imported, _service, principal, root = prepared_analysis
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    transform = (lambda candidate: {**candidate, "covered_source_refs": []}) if invalid_candidate else None
    with model_server(status=http_status, transform=transform) as (origin, requests, _):
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            await configure_model(db, tenant_id=tenant_id, origin=origin)
            proposal = await submitted_proposal(db, prepared_analysis)
            await db.commit()
        analyzer = CompanyKnowledgeAnalyzer(data_root=root)
        result = await analyzer.process(tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker)
        assert result["status"] == "held" and result["error_code"] == expected_error, result
        assert result["can_retry"] is safe_retry
        assert len(requests) == 1
        repeated = await analyzer.process(tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker)
        assert repeated["status"] == "held" and len(requests) == 1
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            if not safe_retry:
                with pytest.raises(CompanyAnalysisGate, match="retry_not_safe"):
                    await analyzer.retry(
                        db, principal=principal, proposal=proposal, trace_id="projection-unknown-retry"
                    )
            else:
                retried = await analyzer.retry(
                    db, principal=principal, proposal=proposal, trace_id="projection-explicit-retry"
                )
                assert retried["status"] == "queued" and retried["attempt"] == 1
                assert len(requests) == 1
            extractions = (await db.scalars(select(CompanyKnowledgeExtraction))).all()
            if invalid_candidate:
                assert len(extractions) == 1 and not extractions[0].validated
                assert extractions[0].model_receipt_json["raw_response"]
            else:
                assert not extractions
            await db.commit()
        if invalid_candidate:
            again = await analyzer.process(tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker)
            assert again["status"] == "held" and len(requests) == 2
            async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
                replay = await analyzer.retry(
                    db, principal=principal, proposal=proposal, trace_id="projection-explicit-retry"
                )
                assert replay["status"] == "held" and replay["attempt"] == 2
                assert len(requests) == 2


async def test_source_revocation_during_generation_preserves_but_quarantines_the_real_reply(
    prepared_analysis,
    app_user_sessionmaker,
    monkeypatch,
):
    import asyncio
    from threading import Event
    from app.models.company_knowledge import CompanyKnowledgeSource
    from app.models.company_knowledge_analysis import CompanyKnowledgeExtraction
    from app.services.company_knowledge_analysis import CompanyKnowledgeAnalyzer
    from sqlalchemy import select

    tenant_id, _user, job_id, imported, _service, _principal, root = prepared_analysis
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    seen, release = Event(), Event()

    def delay(candidate):
        seen.set()
        assert release.wait(10), "Reference provider was not released"
        return candidate

    with model_server(transform=delay) as (origin, requests, _):
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            await configure_model(db, tenant_id=tenant_id, origin=origin)
        task = asyncio.create_task(
            CompanyKnowledgeAnalyzer(data_root=root).process(
                tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker
            )
        )
        try:
            assert await asyncio.to_thread(seen.wait, 5)
            async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
                source = await db.get(CompanyKnowledgeSource, imported.source_id)
                source.status = "retired"
                await db.commit()
        finally:
            release.set()
        result = await task
        assert result["status"] == "held" and result["error_code"] == "company_analysis_source_changed", result
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            extraction = await db.scalar(select(CompanyKnowledgeExtraction))
            assert extraction and not extraction.validated and extraction.model_receipt_json["raw_response"]
            assert len(requests) == 1
            assert await db.scalar(text("SELECT count(*) FROM company_ontology_objects")) == 0


async def test_real_jwt_http_review_consumes_the_analysis_gate_override_and_scoped_metrics(
    prepared_analysis,
    app_user_sessionmaker,
    monkeypatch,
):
    import httpx
    from fastapi import FastAPI
    from app.api import knowledge_company
    from app.config import get_settings
    from app.core.security import create_access_token
    from app.database import get_db

    tenant_id, user_id, _job, imported, _service, _principal, root = prepared_analysis
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(root))
    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        proposal = await submitted_proposal(db, prepared_analysis)
        proposal_id = proposal.id
        await db.commit()
    app = FastAPI()
    app.include_router(knowledge_company.router)

    async def scoped_db():
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            yield db

    app.dependency_overrides[get_db] = scoped_db
    headers = {"Authorization": "Bearer " + create_access_token(str(user_id), "org_admin", str(tenant_id))}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local", headers=headers
    ) as api:
        path = f"/knowledge/company/proposals/{proposal_id}"
        opened = await api.get(path + "/analysis")
        assert opened.status_code == 200 and opened.json()["can_override"] and opened.json()["candidate"] is None, (
            opened.text
        )
        decision = {
            "expected_state_version": 2,
            "decision": "approve",
            "reason": "Human source review",
            "evidence_refs": [f"company-evidence://{imported.evidence_id}"],
            "trace_id": "http-review",
        }
        blocked = await api.post(path + "/review", json=decision)
        assert (
            blocked.status_code == 409 and blocked.json()["detail"]["code"] == "company_analysis_required_before_review"
        ), blocked.text
        from app.models.security_audit import ResourcePermission

        reviewer_id = uuid4()
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            db.add(
                User(
                    id=reviewer_id,
                    tenant_id=tenant_id,
                    role="member",
                    username="scoped-reviewer-" + reviewer_id.hex,
                    email=f"{reviewer_id}@projection.test",
                    display_name="Scoped reviewer",
                    password_hash="synthetic",
                )
            )
            await db.flush()
            db.add(
                ResourcePermission(
                    tenant_id=tenant_id,
                    principal_type="user",
                    principal_id=reviewer_id,
                    resource_type="company_knowledge_scope",
                    resource_id=tenant_id,
                    actions=["review"],
                    effect="allow",
                    sensitivity_ceiling="PL2_pii",
                    purposes=["interactive_session"],
                )
            )
            await db.commit()
        review_headers = {"Authorization": "Bearer " + create_access_token(str(reviewer_id), "member", str(tenant_id))}
        reviewer_view = await api.get(path + "/analysis", headers=review_headers)
        assert reviewer_view.status_code == 200 and reviewer_view.json()["can_override"] is False, reviewer_view.text
        refused = await api.post(
            path + "/analysis/override",
            headers=review_headers,
            json={
                "expected_state_version": 2,
                "reason": "Review alone cannot grant an analysis exception",
                "trace_id": "http-forbidden-override",
            },
        )
        assert refused.status_code == 403, refused.text
        overridden = await api.post(
            path + "/analysis/override",
            json={
                "expected_state_version": 2,
                "reason": "Explicit manual review while analysis is unavailable",
                "trace_id": "http-override",
            },
        )
        assert overridden.status_code == 200 and overridden.json()["state_version"] == 3, overridden.text
        approved = await api.post(path + "/review", json={**decision, "expected_state_version": 3})
        assert approved.status_code == 200 and approved.json()["status"] == "approved", approved.text
        published = await api.post(
            path + "/publish",
            json={
                "expected_state_version": approved.json()["state_version"],
                "valid_from": datetime.now(timezone.utc).isoformat(),
                "valid_until": None,
                "trace_id": "http-publish",
            },
        )
        assert published.status_code == 200 and published.json()["graph_projection_status"] == "skipped_by_override", (
            published.text
        )
        metrics = await api.get("/knowledge/company/analysis/metrics")
        assert (
            metrics.status_code == 200
            and metrics.json()["override_proposals"] == 1
            and metrics.json()["override_rate"] == 1
        )
        # Authentication uses the current user row, not the stale token's role.
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            user = await db.get(User, user_id)
            user.role = "member"
            await db.commit()
        assert (await api.get("/knowledge/company/analysis/metrics")).status_code == 403


async def test_real_comparison_context_cannot_flow_into_a_broader_graph_or_block_original_restoration(
    prepared_analysis,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.models.company_knowledge import CompanyKnowledgeEvidence, CompanyKnowledgePublication
    from app.models.security_audit import ResourcePermission
    from app.services.company_knowledge_analysis import CompanyKnowledgeAnalyzer, CompanyKnowledgeAnalysisReview
    from app.services.company_knowledge_service import CompanyKnowledgeReviewRequest
    from app.services.company_knowledge_indexer import CompanyKnowledgeIndexer
    from app.services.company_knowledge_projection import CompanyKnowledgeProjector
    from app.services.company_knowledge_gateway import CompanyKnowledgeGateway, CompanyKnowledgeReadRequest
    from app.services.company_ontology_gateway import CompanyOntologyGateway, OntologyQueryRequest
    from app.services.company_ontology_service import (
        CompanyOntologyService,
        OntologyPackageInstallRequest,
        OntologyActivationRequest,
    )

    tenant_id, user_id, job_id, imported, service, principal, root = prepared_analysis
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    private_value = "Restricted comparison source detail"
    now = datetime.now(timezone.utc)
    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        evidence = await db.get(CompanyKnowledgeEvidence, imported.evidence_id)
        private = await service.queue_evidence_import(
            db,
            principal=principal,
            request=CompanyEvidenceIngestRequest(
                source_contract_id=evidence.source_contract_id,
                source_contract_version=evidence.source_contract_version,
                evidence_kind="document",
                source_item_id="private-comparison",
                source_revision="one",
                title="Projection source",
                markdown="# Projection source\n" + private_value,
                typed_payload=None,
                external_artifact_ref=None,
                schema_ref="schema://company-document/v1",
                source_acl_snapshot={"user_ids": [str(user_id)]},
                proposed_namespace="company/policies",
                proposed_sensitivity="PL2_pii",
                occurred_at=None,
                effective_from=now,
                effective_until=None,
                observed_at=now,
                cursor={},
                sequence=None,
                coverage_ledger={"complete": True, "total_units": 1, "covered_units": 1, "missing_units": []},
                purpose="Private comparison publication",
                idempotency_key="private-comparison",
                trace_id="private-comparison",
            ),
        )
        await db.commit()
    private = await service.process_import_job(
        tenant_id=tenant_id, job_id=private.id, session_factory=app_user_sessionmaker
    )

    async def approve(db, proposal, trace):
        return await service.record_review(
            db,
            principal=principal,
            proposal_id=proposal.id,
            expected_state_version=proposal.state_version,
            trace_id=trace,
            request=CompanyKnowledgeReviewRequest(
                decision="approve",
                reviewer_role="org_admin",
                reason="Reviewed complete source and context",
                evidence_refs=tuple(proposal.source_refs_json),
                policy_snapshot={},
            ),
        )

    async def publish(db, proposal, trace):
        return await service.publish_proposal(
            db,
            principal=principal,
            proposal_id=proposal.id,
            expected_state_version=proposal.state_version,
            valid_from=now,
            valid_until=None,
            trace_id=trace,
        )

    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        private_proposal = await submitted_proposal(db, (tenant_id, user_id, None, private, service, principal, root))
        await CompanyKnowledgeAnalysisReview(data_root=root).override(
            db,
            principal=principal,
            proposal=private_proposal,
            reason="Manual private-source review",
            trace_id="private-manual-analysis",
        )
        private_publication = await publish(
            db, await approve(db, private_proposal, "private-review"), "private-publish"
        )
        private_id = private_publication.id
        await db.commit()
    await CompanyKnowledgeIndexer().process_pending(
        tenant_id=tenant_id, session_factory=app_user_sessionmaker, limit=20
    )

    def candidate_with_uncited_context(candidate):
        entity = {
            **candidate["entities"][0],
            "properties": {"notes": private_value},
            "source_refs": [f"company-evidence://{imported.evidence_id}"],
        }
        return {**candidate, "entities": [entity]}

    with model_server(transform=candidate_with_uncited_context) as (origin, requests, _):
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            await configure_model(db, tenant_id=tenant_id, origin=origin)
            proposal = await submitted_proposal(db, prepared_analysis)
            proposal_id = proposal.id
            await db.commit()
        result = await CompanyKnowledgeAnalyzer(data_root=root).process(
            tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker
        )
        assert result["status"] == "completed", result
        packet = json.loads(requests[0]["messages"][-1]["content"])
        assert len(packet["comparison_publications"]) == 1
        assert private_value in packet["comparison_publications"][0]["content"]
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            proposal = await service.get_proposal_for_review(db, principal=principal, proposal_id=proposal_id)
            publication = await publish(db, await approve(db, proposal, "context-review"), "context-publish")
            ontology = CompanyOntologyService(knowledge_service=service)
            installation = await ontology.install_package(
                db,
                principal=principal,
                request=OntologyPackageInstallRequest(
                    package_key="project-goal-deliverable-owner",
                    version="1.0.0",
                    idempotency_key="context-install",
                    trace_id="context-install",
                ),
            )
            activation = await ontology.create_activation(
                db,
                principal=principal,
                request=OntologyActivationRequest(
                    installation_id=installation.id,
                    namespace="project",
                    configuration={},
                    idempotency_key="context-activation",
                    trace_id="context-activation",
                ),
            )
            await ontology.dry_run_activation(
                db,
                principal=principal,
                activation_id=activation.id,
                idempotency_key="context-dry-run",
                trace_id="context-dry-run",
            )
            projector = CompanyKnowledgeProjector(data_root=root)
            view = await projector.view(db, principal=principal, publication=publication)
            await projector.confirm_types(
                db,
                principal=principal,
                publication=publication,
                candidate_hash=view["candidate_hash"],
                content_hash=publication.content_hash,
                activation_id=activation.id,
                mapping={
                    "entity_types": {"not_installed.Instrument": "project.Owner"},
                    "property_refs": {"notes": "project.name"},
                    "link_types": {},
                },
                reason="Confirmed installed type",
                trace_id="context-types",
            )
            member_id = uuid4()
            db.add(
                User(
                    id=member_id,
                    tenant_id=tenant_id,
                    role="member",
                    username="context-member-" + member_id.hex,
                    email=f"{member_id}@projection.test",
                    display_name="Broader reader",
                    password_hash="synthetic",
                )
            )
            await db.flush()
            member = principal.__class__(
                tenant_id=tenant_id,
                accountable_user_id=member_id,
                accountable_role="member",
                actor_type="user",
                actor_id=member_id,
                purpose="interactive_session",
            )
            db.add(
                ResourcePermission(
                    tenant_id=tenant_id,
                    principal_type="user",
                    principal_id=member_id,
                    resource_type="company_knowledge_document",
                    resource_id=publication.document_id,
                    actions=["read", "cite", "review"],
                    effect="allow",
                    sensitivity_ceiling="PL2_pii",
                    purposes=["interactive_session"],
                )
            )
            await db.commit()
            public_id = publication.id
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            publication = await db.get(CompanyKnowledgePublication, public_id)
            graph = CompanyOntologyGateway()

            def query():
                return OntologyQueryRequest(
                    namespaces=("project",),
                    object_type_refs=(),
                    object_ids=(),
                    query_ref=None,
                    query_input={},
                    include_facts=True,
                    include_links=True,
                    limit=20,
                    trace_id="context-query:" + str(uuid4()),
                )

            assert (await graph.query(db, principal=principal, request=query())).payload["result_count"] == 1
            assert (await graph.query(db, principal=member, request=query())).payload["result_count"] == 0
            with pytest.raises(PermissionError):
                await service.get_proposal_for_review(db, principal=member, proposal_id=proposal_id)
            denied = await projector.view(db, principal=member, publication=publication)
            assert denied["candidate"] is None and denied["receipt"] is None and denied["context_access"] == "denied"
            await service.retire_publication(
                db,
                principal=principal,
                publication_id=private_id,
                reason="Withdraw comparison source",
                trace_id="context-private-retire",
            )
            assert (await graph.query(db, principal=principal, request=query())).payload["result_count"] == 0
            await service.retire_publication(
                db,
                principal=principal,
                publication_id=public_id,
                reason="Retire original",
                trace_id="context-original-retire",
            )
            restored = await service.restore_publication(
                db,
                principal=principal,
                publication_id=public_id,
                reason="Restore original without unavailable graph context",
                valid_from=datetime.now(timezone.utc),
                trace_id="context-original-restore",
            )
            assert restored.graph_projection_status == "held_context_authority"
            read = await CompanyKnowledgeGateway().read(
                db,
                principal=member,
                request=CompanyKnowledgeReadRequest(
                    document_id=None,
                    publication_id=restored.id,
                    segment_ids=(),
                    max_chars=None,
                    trace_id="context-original-read",
                ),
            )
            assert read.status == "ok"
            queued = await projector.backfill(
                db, principal=principal, publication=restored, trace_id="context-fresh-analysis"
            )
            assert queued["status"] == "queued" and queued["id"] != str(job_id)
            assert len(requests) == 1
            restored_id = restored.id
            await db.commit()
        from app.models.llm import LLMModel
        from app.models.tenant_setting import TenantSetting
        from uuid import UUID

        def fresh_source_only(candidate):
            return {
                **candidate,
                "entities": [{**candidate["entities"][0], "properties": {"notes": "Current source only"}}],
            }

        with model_server(transform=fresh_source_only) as (fresh_origin, fresh_requests, _):
            async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
                setting = await db.scalar(
                    select(TenantSetting).where(
                        TenantSetting.tenant_id == tenant_id, TenantSetting.key == "default_model_id"
                    )
                )
                model = await db.get(LLMModel, UUID(setting.value["model_id"]))
                model.base_url = fresh_origin
                await db.commit()
            completed = await CompanyKnowledgeAnalyzer(data_root=root).process(
                tenant_id=tenant_id, job_id=UUID(queued["id"]), session_factory=app_user_sessionmaker
            )
            assert completed["status"] == "completed", completed
            fresh_packet = json.loads(fresh_requests[0]["messages"][-1]["content"])
            assert not fresh_packet["comparison_publications"] and private_value not in json.dumps(fresh_packet)
            async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
                restored = await db.get(CompanyKnowledgePublication, restored_id)
                fresh_view = await projector.view(db, principal=principal, publication=restored)
                await projector.confirm_types(
                    db,
                    principal=principal,
                    publication=restored,
                    candidate_hash=fresh_view["candidate_hash"],
                    content_hash=restored.content_hash,
                    activation_id=activation.id,
                    mapping={
                        "entity_types": {"not_installed.Instrument": "project.Owner"},
                        "property_refs": {"notes": "project.name"},
                        "link_types": {},
                    },
                    reason="Confirmed the fresh analysis without withdrawn comparison data",
                    trace_id="context-fresh-types",
                )
                assert restored.graph_projection_status == "active"
                visible = await graph.query(db, principal=member, request=query())
                assert visible.payload["result_count"] == 1
                assert visible.payload["objects"][0]["properties"]["project.name"] == "Current source only"
                assert len(requests) == len(fresh_requests) == 1
                await db.commit()


async def test_existing_daemon_drain_reconciles_expired_dispatch_without_resending(
    prepared_analysis,
    app_user_sessionmaker,
):
    from datetime import timedelta
    from app.models.company_knowledge_analysis import CompanyKnowledgeAnalysisJob
    from app.services.company_knowledge_analysis import CompanyKnowledgeAnalyzer
    from app.services.evolution_daemon import _drain_company_kb_jobs
    from app.config import get_settings

    tenant_id, _user_id, job_id, _imported, _service, _principal, root = prepared_analysis
    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        job = await db.get(CompanyKnowledgeAnalysisJob, job_id)
        job.status, job.attempt_count, job.claim_token = "running", 1, uuid4()
        job.available_at = datetime.now(timezone.utc) - timedelta(days=1)
        job.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        job.dispatch_receipt_json = {"status": "dispatching", "attempt": 1}
        await db.commit()
    # The public drain uses the same strict tenant processor after its existing
    # audited cross-tenant discovery. No model is configured for this recovery.
    get_settings_root = get_settings().AGENT_DATA_DIR
    get_settings().AGENT_DATA_DIR = str(root)
    try:
        await _drain_company_kb_jobs()
    finally:
        get_settings().AGENT_DATA_DIR = get_settings_root
    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        assert (await db.get(CompanyKnowledgeAnalysisJob, job_id)).status == "held"
    result = await CompanyKnowledgeAnalyzer(data_root=root).process(
        tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker
    )
    assert result["status"] == "held" and result["error_code"] == "company_analysis_provider_execution_unknown"
    assert not result["can_retry"] and result["attempt"] == 1


async def test_inferred_model_can_be_explicitly_selected_before_analysis_recovery(
    prepared_analysis,
    app_user_sessionmaker,
    monkeypatch,
):
    import httpx
    from fastapi import FastAPI
    from app.api import enterprise, knowledge_company
    from app.config import get_settings
    from app.core.security import create_access_token
    from app.database import get_db
    from app.models.llm import LLMModel
    from app.services.company_knowledge_analysis import CompanyKnowledgeAnalyzer
    from app.services.secrets_provider import get_secrets_provider

    tenant_id, user_id, job_id, _imported, _service, _principal, root = prepared_analysis
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(root))
    app = FastAPI()
    app.include_router(enterprise.router)
    app.include_router(knowledge_company.router)

    async def scoped_db():
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            yield db

    app.dependency_overrides[get_db] = scoped_db
    with model_server() as (origin, requests, _):
        model_id = uuid4()
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            db.add(
                LLMModel(
                    id=model_id,
                    tenant_id=tenant_id,
                    provider="openai",
                    model="analysis-reference-v1",
                    label="Single available model",
                    base_url=origin,
                    enabled=True,
                    max_output_tokens=8096,
                    api_key_encrypted=get_secrets_provider().encrypt("synthetic-company-model-key"),
                )
            )
            proposal = await submitted_proposal(db, prepared_analysis)
            proposal_id = proposal.id
            await db.commit()
        analyzer = CompanyKnowledgeAnalyzer(data_root=root)
        held = await analyzer.process(tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker)
        assert held["error_code"] == "company_analysis_configured_model_unavailable" and not requests
        headers = {"Authorization": "Bearer " + create_access_token(str(user_id), "org_admin", str(tenant_id))}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local", headers=headers
        ) as api:
            inferred = await api.get("/enterprise/llm-models")
            assert inferred.status_code == 200 and inferred.json()[0]["is_default"] is True
            assert inferred.json()[0]["default_is_explicit"] is False
            selected = await api.put("/enterprise/llm-models/default", json={"model_id": str(model_id)})
            assert selected.status_code == 200, selected.text
            confirmed = await api.get("/enterprise/llm-models")
            assert confirmed.json()[0]["default_is_explicit"] is True
            retry = await api.post(
                f"/knowledge/company/proposals/{proposal_id}/analysis/retry",
                json={"expected_state_version": 2, "trace_id": "explicit-default-analysis-retry"},
            )
            assert retry.status_code == 200 and retry.json()["status"] == "queued", retry.text
            assert not requests
            completed = await analyzer.process(
                tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker
            )
            assert completed["status"] == "completed" and len(requests) == 1, completed
            workspace = await api.get(f"/knowledge/company/proposals/{proposal_id}/analysis")
            assert workspace.status_code == 200 and workspace.json()["candidate"], workspace.text


async def test_accepted_analysis_survives_uploader_departure_for_a_current_authorized_reviewer(
    prepared_analysis,
    app_user_sessionmaker,
    monkeypatch,
):
    import httpx
    from fastapi import FastAPI
    from app.api import knowledge_company
    from app.config import get_settings
    from app.core.security import create_access_token
    from app.database import get_db
    from app.models.company_knowledge_analysis import CompanyKnowledgeAnalysisJob
    from app.services.company_knowledge_analysis import CompanyKnowledgeAnalyzer

    tenant_id, uploader_id, job_id, imported, service, principal, root = prepared_analysis
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(root))
    with model_server() as (origin, requests, _):
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            await configure_model(db, tenant_id=tenant_id, origin=origin)
            proposal = await submitted_proposal(db, prepared_analysis)
            proposal_id = proposal.id
            await db.commit()
        result = await CompanyKnowledgeAnalyzer(data_root=root).process(
            tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker
        )
        assert result["status"] == "completed"
        reviewer_id = uuid4()
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            uploader = await db.get(User, uploader_id)
            uploader.is_active = False
            db.add(
                User(
                    id=reviewer_id,
                    tenant_id=tenant_id,
                    role="org_admin",
                    username="replacement-" + reviewer_id.hex,
                    email=f"{reviewer_id}@projection.test",
                    display_name="Current reviewer",
                    password_hash="synthetic",
                )
            )
            await db.commit()
        app = FastAPI()
        app.include_router(knowledge_company.router)

        async def scoped_db():
            async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
                yield db

        app.dependency_overrides[get_db] = scoped_db
        headers = {"Authorization": "Bearer " + create_access_token(str(reviewer_id), "org_admin", str(tenant_id))}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local", headers=headers
        ) as api:
            path = f"/knowledge/company/proposals/{proposal_id}"
            view = await api.get(path + "/analysis")
            assert view.status_code == 200 and view.json()["candidate"] is not None, view.text
            approved = await api.post(
                path + "/review",
                json={
                    "expected_state_version": 2,
                    "decision": "approve",
                    "reason": "Reviewed the accepted analysis and source under current authority",
                    "evidence_refs": [f"company-evidence://{imported.evidence_id}"],
                    "trace_id": "replacement-review",
                },
            )
            assert approved.status_code == 200 and approved.json()["status"] == "approved", approved.text
            published = await api.post(
                path + "/publish",
                json={
                    "expected_state_version": approved.json()["state_version"],
                    "valid_from": datetime.now(timezone.utc).isoformat(),
                    "valid_until": None,
                    "trace_id": "replacement-publish",
                },
            )
            assert published.status_code == 200 and published.json()["published_by_user_id"] == str(reviewer_id), (
                published.text
            )
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            job = await db.get(CompanyKnowledgeAnalysisJob, job_id)
            assert job.accountable_user_id == uploader_id and job.status == "completed"
            assert len(requests) == 1


async def test_analysis_output_limit_holds_without_a_second_larger_generation(
    prepared_analysis,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.models.company_knowledge_analysis import CompanyKnowledgeExtraction
    from app.services.company_knowledge_analysis import CompanyKnowledgeAnalyzer

    tenant_id, _user_id, job_id, _imported, _service, _principal, root = prepared_analysis
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    with model_server(finish_reason="length") as (origin, requests, _):
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            await configure_model(db, tenant_id=tenant_id, origin=origin)
        result = await CompanyKnowledgeAnalyzer(data_root=root).process(
            tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker
        )
        assert len(requests) == 1, [request.get("max_completion_tokens") for request in requests]
        assert requests[0]["max_completion_tokens"] == 8096
        assert result["status"] == "held" and result["error_code"] == "company_analysis_output_incomplete", result
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            extraction = await db.scalar(
                select(CompanyKnowledgeExtraction).where(CompanyKnowledgeExtraction.job_id == job_id)
            )
            assert not extraction.validated and extraction.model_receipt_json["finish_reason"] == "length"
            assert extraction.model_receipt_json["raw_response"]


async def test_an_exact_proposal_review_grant_can_consume_its_accepted_analysis(
    prepared_analysis,
    app_user_sessionmaker,
    monkeypatch,
):
    import httpx
    from fastapi import FastAPI
    from app.api import knowledge_company
    from app.config import get_settings
    from app.core.security import create_access_token
    from app.database import get_db
    from app.models.security_audit import ResourcePermission
    from app.services.company_knowledge_analysis import CompanyKnowledgeAnalyzer

    tenant_id, _user_id, job_id, imported, service, principal, root = prepared_analysis
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(root))
    with model_server() as (origin, requests, _):
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            await configure_model(db, tenant_id=tenant_id, origin=origin)
            proposal = await submitted_proposal(db, prepared_analysis)
            proposal_id = proposal.id
            reviewer_id = uuid4()
            db.add(
                User(
                    id=reviewer_id,
                    tenant_id=tenant_id,
                    role="member",
                    username="exact-reviewer-" + reviewer_id.hex,
                    email=f"{reviewer_id}@projection.test",
                    display_name="Candidate reviewer",
                    password_hash="synthetic",
                )
            )
            await db.flush()
            db.add(
                ResourcePermission(
                    tenant_id=tenant_id,
                    principal_type="user",
                    principal_id=reviewer_id,
                    resource_type="company_knowledge_proposal",
                    resource_id=proposal_id,
                    actions=["review"],
                    effect="allow",
                    sensitivity_ceiling="PL2_pii",
                    purposes=["interactive_session"],
                )
            )
            await db.commit()
        result = await CompanyKnowledgeAnalyzer(data_root=root).process(
            tenant_id=tenant_id, job_id=job_id, session_factory=app_user_sessionmaker
        )
        assert result["status"] == "completed"
        app = FastAPI()
        app.include_router(knowledge_company.router)

        async def scoped_db():
            async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
                yield db

        app.dependency_overrides[get_db] = scoped_db
        headers = {"Authorization": "Bearer " + create_access_token(str(reviewer_id), "member", str(tenant_id))}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local", headers=headers
        ) as api:
            path = f"/knowledge/company/proposals/{proposal_id}"
            view = await api.get(path + "/analysis")
            assert view.status_code == 200 and view.json()["candidate"] is not None, view.text
            changes = await api.post(
                path + "/review",
                json={
                    "expected_state_version": 2,
                    "decision": "request_changes",
                    "reason": "The scoped reviewer requests an evidence clarification",
                    "evidence_refs": [f"company-evidence://{imported.evidence_id}"],
                    "trace_id": "exact-scoped-review",
                },
            )
            assert changes.status_code == 200 and changes.json()["status"] == "changes_requested", changes.text
            assert len(requests) == 1
