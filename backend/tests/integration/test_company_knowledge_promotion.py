from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import uuid

import pytest
from sqlalchemy import func, select

from app.models.company_knowledge import (
    CompanyKnowledgeEvidence,
    CompanyKnowledgeImportJob,
    CompanyKnowledgeProposal,
    CompanyKnowledgePublication,
    CompanyKnowledgeSource,
)
from app.models.knowledge import KnowledgeDocument, KnowledgeSegment
from app.models.security_audit import ResourcePermission
from app.models.tenant import Tenant
from app.models.user import User
from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal
from app.services.company_knowledge_promotion import (
    CompanyKnowledgePromotionService,
    LegacyPromotionIntakeRequest,
    PersonalPromotionIntakeRequest,
)
from app.services.company_knowledge_service import (
    CompanyKnowledgeMaterializationRequest,
    CompanyKnowledgeReviewRequest,
    CompanyKnowledgeService,
)


def _principal(*, tenant_id: uuid.UUID, user_id: uuid.UUID, role: str = "org_admin") -> CompanyKnowledgePrincipal:
    return CompanyKnowledgePrincipal(
        tenant_id=tenant_id,
        accountable_user_id=user_id,
        accountable_role=role,
        actor_type="user",
        actor_id=user_id,
        purpose="interactive_session",
        session_id="company-promotion-integration",
    )


@pytest.mark.asyncio
async def test_personal_and_legacy_promotion_use_recoverable_evidence_to_review_handoff(
    owner_sessionmaker,
    tmp_path: Path,
) -> None:
    tenant_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    denied_id = uuid.uuid4()
    personal_document_id = uuid.uuid4()
    personal_markdown = "# Owner note\n\nThis exact note is approved for Company review.\n"
    personal_hash = hashlib.sha256(personal_markdown.encode("utf-8")).hexdigest()
    personal_relative_path = (
        Path("persons") / str(admin_id) / "kb" / "documents" / personal_hash[:2] / f"{personal_hash}.md"
    )
    personal_artifact = tmp_path / personal_relative_path
    personal_artifact.parent.mkdir(parents=True)
    personal_artifact.write_text(personal_markdown, encoding="utf-8")

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Promotion Tenant", slug=f"promotion-{tenant_id.hex[:10]}"))
        for user_id, suffix in ((admin_id, "admin"), (denied_id, "denied")):
            db.add(
                User(
                    id=user_id,
                    username=f"promotion-{suffix}-{user_id.hex[:10]}",
                    email=f"{user_id.hex[:10]}@promotion.test",
                    password_hash="x",
                    display_name=f"Promotion {suffix}",
                    role="org_admin",
                    tenant_id=tenant_id,
                )
            )
        await db.flush()
        db.add(
            ResourcePermission(
                tenant_id=tenant_id,
                principal_type="user",
                principal_id=admin_id,
                resource_type="company_knowledge_scope",
                resource_id=tenant_id,
                actions=[
                    "approve",
                    "publish",
                    "discover",
                    "search",
                    "read",
                    "cite",
                    "propose",
                    "review",
                ],
                conditions={},
                effect="allow",
                sensitivity_ceiling="PL3_sensitive",
                purposes=["interactive_session"],
                created_by_user_id=admin_id,
            )
        )
        personal_document = KnowledgeDocument(
            id=personal_document_id,
            tenant_id=tenant_id,
            scope_type="person",
            scope_id=admin_id,
            owner_user_id=admin_id,
            source_kind="owner_note",
            source_uri="owner-note://promotion",
            source_sha256="a" * 64,
            artifact_hash=personal_hash,
            title="Owner note",
            status="ready",
            sensitivity="PL2_pii",
            agent_searchable=True,
            canonical_md_path=personal_relative_path.as_posix(),
            canonical_md_sha256=personal_hash,
            doc_metadata_json={},
            created_by_user_id=admin_id,
        )
        db.add(personal_document)
        await db.flush()
        db.add(
            KnowledgeSegment(
                tenant_id=tenant_id,
                document_id=personal_document_id,
                scope_type="person",
                scope_id=admin_id,
                position=0,
                segment_hash=hashlib.sha256(b"personal-promotion-segment").hexdigest(),
                heading_path_json=["Owner note"],
                content="This exact note is approved for Company review.",
                token_count=9,
                segment_metadata_json={},
            )
        )
        await db.commit()

    company_service = CompanyKnowledgeService(data_root=tmp_path)
    promotion_service = CompanyKnowledgePromotionService(
        data_root=tmp_path,
        company_service=company_service,
    )
    principal = _principal(tenant_id=tenant_id, user_id=admin_id)
    # PDEC-013: the grant-boundary negative is an unprivileged member; a
    # company administrator now holds role-sourced business access.
    denied_principal = _principal(tenant_id=tenant_id, user_id=denied_id, role="member")

    personal_request = PersonalPromotionIntakeRequest(
        document_id=personal_document_id,
        proposed_namespace="company/team-notes",
        purpose="Promote an owner-reviewed operating note",
        risk_level="normal",
        title=None,
        attest_scope_change=True,
        idempotency_key="personal-promotion-integration",
        trace_id="trace-personal-promotion-integration",
    )

    async def queue_personal() -> uuid.UUID:
        async with owner_sessionmaker() as db:
            job = await promotion_service.queue_personal_promotion(
                db,
                principal=principal,
                request=personal_request,
            )
            job_id = job.id
            await db.commit()
            return job_id

    personal_job_ids = await asyncio.gather(queue_personal(), queue_personal())
    assert personal_job_ids[0] == personal_job_ids[1]
    personal_job_id = personal_job_ids[0]

    async with owner_sessionmaker() as db:
        personal_job = await db.get(CompanyKnowledgeImportJob, personal_job_id)
        assert personal_job is not None
        personal_job_artifact = Path(str(personal_job.artifact_ref))
        assert (
            await db.scalar(
                select(func.count(CompanyKnowledgeImportJob.id)).where(
                    CompanyKnowledgeImportJob.tenant_id == tenant_id,
                )
            )
            == 1
        )

    personal_job_artifact.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact_hash_mismatch"):
        await company_service.process_import_job(
            tenant_id=tenant_id,
            job_id=personal_job_id,
            session_factory=owner_sessionmaker,
        )

    async with owner_sessionmaker() as db:
        failed_job = await db.get(CompanyKnowledgeImportJob, personal_job_id)
        assert failed_job is not None
        assert failed_job.status == "queued"
        assert failed_job.proposal_id is None
        assert (
            await db.scalar(
                select(func.count(CompanyKnowledgeProposal.id)).where(CompanyKnowledgeProposal.tenant_id == tenant_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count(CompanyKnowledgeEvidence.id)).where(CompanyKnowledgeEvidence.tenant_id == tenant_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count(CompanyKnowledgeSource.id)).where(CompanyKnowledgeSource.tenant_id == tenant_id)
            )
            == 0
        )

    personal_job_artifact.write_text(personal_markdown, encoding="utf-8")
    processed_personal = await company_service.process_import_job(
        tenant_id=tenant_id,
        job_id=personal_job_id,
        session_factory=owner_sessionmaker,
    )
    assert processed_personal.status == "completed"
    assert processed_personal.proposal_id is not None

    async with owner_sessionmaker() as db:
        personal_proposal = await db.get(CompanyKnowledgeProposal, processed_personal.proposal_id)
        assert personal_proposal is not None
        assert personal_proposal.proposal_kind == "personal_promotion"
        assert personal_proposal.status == "submitted"
        assert personal_proposal.source_id == processed_personal.source_id
        assert personal_proposal.source_document_id == processed_personal.document_id
        assert personal_proposal.source_refs_json == [f"company-evidence://{processed_personal.evidence_id}"]
        assert "markdown" not in personal_proposal.proposed_patch_json
        assert personal_proposal.proposed_patch_json["content_ref"] == (f"company-import://{personal_job_id}/candidate")
        candidate = await promotion_service.get_candidate(
            db,
            principal=principal,
            job_id=personal_job_id,
        )
        assert candidate["markdown"] == personal_markdown
        assert candidate["content_hash"] == personal_hash
        with pytest.raises(LookupError):
            await promotion_service.get_intake(
                db,
                principal=denied_principal,
                job_id=personal_job_id,
            )
        with pytest.raises(PermissionError):
            await promotion_service.get_candidate(
                db,
                principal=denied_principal,
                job_id=personal_job_id,
            )

        materialized = await company_service.materialize_proposal(
            db,
            principal=principal,
            proposal_id=personal_proposal.id,
            request=CompanyKnowledgeMaterializationRequest(
                title=candidate["title"],
                markdown=candidate["markdown"],
                expected_proposed_content_hash=personal_proposal.proposed_content_hash,
                attest_candidate_applied=True,
                idempotency_key="personal-promotion-materialized",
            ),
            expected_state_version=personal_proposal.state_version,
            trace_id="trace-personal-promotion-materialized",
        )
        reviewed = await company_service.record_review(
            db,
            principal=principal,
            proposal_id=materialized.id,
            request=CompanyKnowledgeReviewRequest(
                decision="approve",
                reviewer_role="org_admin",
                reason="Exact Personal candidate reviewed",
                evidence_refs=tuple(materialized.source_refs_json),
                policy_snapshot={},
            ),
            expected_state_version=materialized.state_version,
            trace_id="trace-personal-promotion-review",
        )
        publication = await company_service.publish_proposal(
            db,
            principal=principal,
            proposal_id=reviewed.id,
            expected_state_version=reviewed.state_version,
            valid_from=datetime.now(timezone.utc),
            valid_until=None,
            trace_id="trace-personal-promotion-publish",
        )
        assert publication.status == "active"
        await db.commit()

    legacy_dir = tmp_path / f"enterprise_info_{tenant_id}"
    legacy_dir.mkdir()
    legacy_source = legacy_dir / "knowledge_base" / "retired.md"
    legacy_source.parent.mkdir()
    legacy_source.write_text("# Retired process\n\nUse the reviewed process.\n", encoding="utf-8")
    legacy_hash = hashlib.sha256(legacy_source.read_bytes()).hexdigest()
    legacy_request = LegacyPromotionIntakeRequest(
        relative_path="knowledge_base/retired.md",
        expected_sha256=legacy_hash,
        proposed_namespace="company/processes",
        proposed_sensitivity="PL2_pii",
        purpose="Review a retired process before Company publication",
        risk_level="normal",
        title="Retired process",
        attest_scope_change=True,
        idempotency_key="legacy-promotion-integration",
        trace_id="trace-legacy-promotion-integration",
    )

    async def queue_legacy() -> uuid.UUID:
        async with owner_sessionmaker() as db:
            job = await promotion_service.queue_legacy_promotion(
                db,
                principal=principal,
                company_dir=legacy_dir,
                request=legacy_request,
            )
            job_id = job.id
            await db.commit()
            return job_id

    legacy_job_ids = await asyncio.gather(queue_legacy(), queue_legacy())
    assert legacy_job_ids[0] == legacy_job_ids[1]
    legacy_job_id = legacy_job_ids[0]

    async with owner_sessionmaker() as db:
        legacy_job = await db.get(CompanyKnowledgeImportJob, legacy_job_id)
        assert legacy_job is not None
        assert (
            await db.scalar(
                select(func.count(CompanyKnowledgeImportJob.id)).where(
                    CompanyKnowledgeImportJob.tenant_id == tenant_id,
                )
            )
            == 2
        )
        legacy_job.status = "failed"
        legacy_job.attempt_count = legacy_job.max_attempts
        legacy_job.last_error_code = "simulated_provider_outage"
        await db.commit()

    async with owner_sessionmaker() as db:
        retry_view = await promotion_service.retry_intake(
            db,
            principal=principal,
            job_id=legacy_job_id,
            trace_id="trace-legacy-promotion-retry",
        )
        retried = await db.get(CompanyKnowledgeImportJob, legacy_job_id)
        assert retried is not None
        assert retry_view["status"] == "queued"
        assert retried.max_attempts == retried.attempt_count + 3
        await db.commit()

    processed_legacy = await company_service.process_import_job(
        tenant_id=tenant_id,
        job_id=legacy_job_id,
        session_factory=owner_sessionmaker,
    )
    assert processed_legacy.status == "completed"
    assert processed_legacy.proposal_id is not None

    async with owner_sessionmaker() as db:
        legacy_proposal = await db.get(CompanyKnowledgeProposal, processed_legacy.proposal_id)
        assert legacy_proposal is not None
        assert legacy_proposal.proposal_kind == "legacy_import"
        assert legacy_proposal.status == "submitted"
        legacy_candidate = await promotion_service.get_candidate(
            db,
            principal=principal,
            job_id=legacy_job_id,
        )
        assert "Retired process" in legacy_candidate["markdown"]
        intakes = await promotion_service.list_intakes(
            db,
            principal=principal,
            kind=None,
            limit=10,
        )
        assert {item["kind"] for item in intakes} == {
            "personal_promotion",
            "legacy_import",
        }
        assert (
            await db.scalar(
                select(func.count(CompanyKnowledgePublication.id)).where(
                    CompanyKnowledgePublication.tenant_id == tenant_id,
                    CompanyKnowledgePublication.status == "active",
                )
            )
            == 1
        )
