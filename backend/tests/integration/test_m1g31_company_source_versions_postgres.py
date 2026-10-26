"""Company source receipts use exact immutable publications and current readers."""

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.company_knowledge import CompanyKnowledgePublication
from app.services.company_knowledge_gateway import (
    CompanyKnowledgeGateway,
    CompanyKnowledgeReadRequest,
    CompanyKnowledgeSearchRequest,
)
from tests.integration.test_company_knowledge_direct_import import (
    _seed_tenant,
    _register_contract,
    _queue_file,
    _process,
    _principal,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def published_source(owner_sessionmaker, monkeypatch, tmp_path):
    from app.config import get_settings
    from app.services.company_knowledge_analysis import CompanyKnowledgeAnalysisReview
    from app.services.company_knowledge_service import CompanyKnowledgeService, CompanyKnowledgeReviewRequest
    from app.models.chat_session import ChatSession

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    ids = await _seed_tenant(owner_sessionmaker)
    service = CompanyKnowledgeService(data_root=tmp_path)
    contract = await _register_contract(owner_sessionmaker, service, tenant_id=ids["tenant"], admin_id=ids["admin"])
    job = await _queue_file(
        owner_sessionmaker,
        service,
        tenant_id=ids["tenant"],
        admin_id=ids["admin"],
        contract=contract,
        filename="source.md",
        data=b"# COMPANY_SOURCE_MARKER\nApproved publication body\n",
        title="Source",
        mime="text/markdown",
        idempotency_key=f"source-{uuid.uuid4().hex}",
    )
    processed = await _process(owner_sessionmaker, service, tenant_id=ids["tenant"], job_id=job.id)
    assert processed.status == "completed", processed.last_error
    principal = _principal(tenant_id=ids["tenant"], user_id=ids["admin"])
    async with owner_sessionmaker() as db:
        proposal = await service.create_proposal_from_import(
            db, principal=principal, job_id=job.id, trace_id="source-proposal"
        )
        await CompanyKnowledgeAnalysisReview(data_root=tmp_path).override(
            db,
            principal=principal,
            proposal=proposal,
            reason="Fixture administrator inspected the complete imported source",
            trace_id="source-analysis-review",
        )
        reviewed = await service.record_review(
            db,
            principal=principal,
            proposal_id=proposal.id,
            request=CompanyKnowledgeReviewRequest(
                decision="approve",
                reviewer_role="org_admin",
                reason="Fixture approval",
                evidence_refs=(f"company-evidence://{processed.evidence_id}",),
                policy_snapshot={},
            ),
            expected_state_version=proposal.state_version,
            trace_id="source-review",
        )
        publication = await service.publish_proposal(
            db,
            principal=principal,
            proposal_id=proposal.id,
            expected_state_version=reviewed.state_version,
            valid_from=datetime.now(timezone.utc),
            valid_until=None,
            trace_id="source-publish",
        )
        session = uuid.uuid4()
        db.add(ChatSession(id=session, tenant_id=ids["tenant"], agent_id=ids["agent"], user_id=ids["admin"]))
        await db.commit()
        return ids, publication.id, publication.content_hash, session, tmp_path


async def test_gateway_read_and_search_export_canonical_publication_version(published_source, app_user_sessionmaker):
    from app.services.knowledge_provenance import build_knowledge_provenance

    ids, publication, version, _session, _root = published_source
    principal = _principal(tenant_id=ids["tenant"], user_id=ids["admin"])
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, ids["tenant"])
        gateway = CompanyKnowledgeGateway()
        result = await gateway.read(
            db,
            principal=principal,
            request=CompanyKnowledgeReadRequest(
                document_id=None, publication_id=publication, segment_ids=(), max_chars=None, trace_id="source-read"
            ),
        )
        assert result.status == "ok", result.as_dict()
        assert result.as_dict()["source_version"] == version
        provenance = build_knowledge_provenance("read_company_kb", result.as_json())
        assert {s["source_version"] for s in provenance["sources"]} == {version}
        found = await gateway.search(
            db,
            principal=principal,
            request=CompanyKnowledgeSearchRequest(
                query="Approved publication body", filters={}, limit=5, trace_id="source-search"
            ),
        )
        assert found.results
        assert {r.as_dict()["source_version"] for r in found.results} == {version}


async def test_read_never_labels_concurrent_rebuild_segments_as_the_old_publication(
    published_source,
    app_user_sessionmaker,
    owner_sessionmaker,
    monkeypatch,
):
    from app.models.knowledge import KnowledgeDocument, KnowledgeSegment

    ids, publication, _version, _session, _root = published_source
    principal = _principal(tenant_id=ids["tenant"], user_id=ids["admin"])
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, ids["tenant"])
        execute = db.execute
        changed = False

        async def interleave(statement, *args, **kwargs):
            nonlocal changed
            columns = getattr(statement, "column_descriptions", ())
            if not changed and len(columns) == 1 and columns[0].get("entity") is KnowledgeSegment:
                changed = True
                async with owner_sessionmaker() as writer:
                    pub = await writer.get(CompanyKnowledgePublication, publication)
                    doc = await writer.get(KnowledgeDocument, pub.document_id)
                    doc.canonical_md_sha256 = doc.source_sha256 = "f" * 64
                    for segment in (
                        await writer.scalars(
                            select(KnowledgeSegment).where(KnowledgeSegment.document_id == pub.document_id)
                        )
                    ).all():
                        segment.content = "NEW_UNPUBLISHED_SEGMENT_BODY"
                    await writer.commit()
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", interleave)
        result = await CompanyKnowledgeGateway().read(
            db,
            principal=principal,
            request=CompanyKnowledgeReadRequest(
                document_id=None,
                publication_id=publication,
                segment_ids=(),
                max_chars=None,
                trace_id="concurrent-source-read",
            ),
        )
        assert changed
        assert "NEW_UNPUBLISHED_SEGMENT_BODY" not in result.as_json(), result.as_dict()
        found = await CompanyKnowledgeGateway().search(
            db,
            principal=principal,
            request=CompanyKnowledgeSearchRequest(
                query="NEW_UNPUBLISHED_SEGMENT_BODY", filters={}, limit=5, trace_id="concurrent-source-search"
            ),
        )
        assert not found.results, found.as_dict()


async def test_runtime_revalidation_never_inherits_browser_admin_inspection(
    published_source,
    app_user_sessionmaker,
    owner_sessionmaker,
):
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.services.content_input_receipts import (
        ContentInputReceiptError,
        validate_runtime_input_dependencies,
        validate_browser_input_dependencies,
    )
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    ids, publication, version, session, root = published_source
    dependency = [
        {"source_kind": "company_knowledge_publication", "source_id": str(publication), "source_version": version}
    ]
    request = ToolExecutionRequest(
        tool_name="read_file",
        arguments={"path": "workspace/derived.md"},
        context=ToolExecutionContext(
            agent_id=ids["agent"],
            user_id=ids["admin"],
            tenant_id=str(ids["tenant"]),
            session_id=str(session),
            workspace=root,
        ),
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, ids["tenant"])
        await validate_runtime_input_dependencies(db, request, dependency)
        await db.commit()
        async with owner_sessionmaker() as writer:
            permissions = (
                await writer.scalars(select(ResourcePermission).where(ResourcePermission.tenant_id == ids["tenant"]))
            ).all()
            for permission in permissions:
                permission.effect = "deny"
            await writer.commit()
        user = await db.get(User, ids["admin"])
        await validate_browser_input_dependencies(db, ids["tenant"], user, dependency)
        with pytest.raises(ContentInputReceiptError, match="no longer authorized"):
            await validate_runtime_input_dependencies(db, request, dependency)


@pytest.mark.parametrize("change", ["version", "retire", "bytes"])
async def test_publication_dependency_has_no_current_draft_or_byte_fallback(
    published_source,
    app_user_sessionmaker,
    owner_sessionmaker,
    change,
):
    from app.models.knowledge import KnowledgeDocument
    from app.models.user import User
    from app.services.content_input_receipts import ContentInputReceiptError, validate_browser_input_dependencies

    ids, publication_id, version, _session, root = published_source
    if change == "version":
        version = "0" * 64
    async with owner_sessionmaker() as db:
        publication = await db.get(CompanyKnowledgePublication, publication_id)
        if change == "retire":
            publication.status = "retired"
        if change == "bytes":
            document = await db.get(KnowledgeDocument, publication.document_id)
            (root / Path(document.canonical_md_path)).write_text("UNVERIFIED_CANONICAL_BYTES")
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, ids["tenant"])
        with pytest.raises(ContentInputReceiptError):
            await validate_browser_input_dependencies(
                db,
                ids["tenant"],
                await db.get(User, ids["admin"]),
                [
                    {
                        "source_kind": "company_knowledge_publication",
                        "source_id": str(publication_id),
                        "source_version": version,
                    }
                ],
            )
