from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest
from sqlalchemy import func, select

from app.models.company_knowledge import (
    CompanyKnowledgeEvidence,
    CompanyKnowledgeEvent,
    CompanyKnowledgeImportJob,
    CompanyKnowledgeOutbox,
    CompanyKnowledgePublication,
    CompanyKnowledgeSource,
)
from app.models.knowledge import KnowledgeDocument, KnowledgeSegment
from app.models.security_audit import ResourcePermission
from app.models.tenant import Tenant
from app.models.user import User
from app.services.company_knowledge_contracts import SourceContractInput
from app.services.company_knowledge_evidence import verify_company_knowledge_event_chain
from app.services.company_knowledge_indexer import CompanyKnowledgeIndexer
from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal
from app.services.company_knowledge_service import (
    CompanyEvidenceIngestRequest,
    CompanyKnowledgeProposalRequest,
    CompanyKnowledgeReviewRequest,
    CompanyKnowledgeService,
)


def _principal(*, tenant_id: uuid.UUID, user_id: uuid.UUID) -> CompanyKnowledgePrincipal:
    return CompanyKnowledgePrincipal(
        tenant_id=tenant_id,
        accountable_user_id=user_id,
        accountable_role="org_admin",
        actor_type="user",
        actor_id=user_id,
        purpose="interactive_session",
        session_id="company-kb-integration",
    )


def _contract() -> SourceContractInput:
    return SourceContractInput(
        source_kind="document",
        provider_kind="native",
        stable_source_id="employee-handbook",
        owner_principal_ref="role:org_admin",
        accountable_steward_ref="role:org_admin",
        connection_ref=None,
        schema_ref="schema://company-document/v1",
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
        allowed_namespaces=("company/policies",),
        precedence_policy_ref=None,
        acceptance_suite_ref="acceptance://company-document/v1",
        idempotency_policy={"key": "source_item_id+revision"},
    )


@pytest.mark.asyncio
async def test_company_document_ingest_review_publish_index_retire_restore_closed_loop(
    owner_sessionmaker,
    tmp_path: Path,
) -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    service = CompanyKnowledgeService(data_root=tmp_path)
    principal = _principal(tenant_id=tenant_id, user_id=user_id)

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Company KB", slug=f"company-kb-{tenant_id.hex[:10]}"))
        db.add(
            User(
                id=user_id,
                username=f"company-kb-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@company-kb.test",
                password_hash="x",
                display_name="Company Knowledge Admin",
                role="org_admin",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            ResourcePermission(
                tenant_id=tenant_id,
                principal_type="user",
                principal_id=user_id,
                resource_type="company_knowledge_scope",
                resource_id=tenant_id,
                actions=["approve", "publish", "retire", "restore", "search", "read", "cite"],
                conditions={},
                effect="allow",
                sensitivity_ceiling="PL3_sensitive",
                purposes=["interactive_session"],
                created_by_user_id=user_id,
            )
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        contract = await service.register_source_contract(
            db,
            principal=principal,
            contract_input=_contract(),
            idempotency_key="contract:employee-handbook:v1",
            trace_id="trace-contract",
        )
        await db.commit()

        job = await service.queue_evidence_import(
            db,
            principal=principal,
            request=CompanyEvidenceIngestRequest(
                source_contract_id=contract.id,
                source_contract_version=1,
                evidence_kind="document",
                source_item_id="employee-handbook",
                source_revision="2026-07-24",
                title="Employee Handbook",
                markdown="# Leave Policy\n\nEmployees receive 20 days of annual leave.",
                typed_payload=None,
                external_artifact_ref=None,
                schema_ref="schema://company-document/v1",
                source_acl_snapshot={"role_names": ["member", "org_admin"]},
                proposed_namespace="company/policies",
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
                purpose="publish employee leave policy",
                idempotency_key="import:employee-handbook:2026-07-24",
                trace_id="trace-import",
            ),
        )
        await db.commit()

        replayed_job = await service.queue_evidence_import(
            db,
            principal=principal,
            request=CompanyEvidenceIngestRequest(
                source_contract_id=contract.id,
                source_contract_version=1,
                evidence_kind="document",
                source_item_id="employee-handbook",
                source_revision="2026-07-24",
                title="Employee Handbook",
                markdown="# Leave Policy\n\nEmployees receive 20 days of annual leave.",
                typed_payload=None,
                external_artifact_ref=None,
                schema_ref="schema://company-document/v1",
                source_acl_snapshot={"role_names": ["member", "org_admin"]},
                proposed_namespace="company/policies",
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
                purpose="publish employee leave policy",
                idempotency_key="import:employee-handbook:2026-07-24",
                trace_id="trace-import",
            ),
        )
        assert replayed_job.id == job.id

    processed = await service.process_import_job(
        tenant_id=tenant_id,
        job_id=job.id,
        session_factory=owner_sessionmaker,
    )
    assert processed.status == "completed"
    assert processed.document_id is not None
    assert processed.evidence_id is not None

    async with owner_sessionmaker() as db:
        proposal = await service.create_proposal(
            db,
            principal=principal,
            request=CompanyKnowledgeProposalRequest(
                proposal_kind="knowledge",
                source_id=processed.source_id,
                source_document_id=processed.document_id,
                source_revision_ref="2026-07-24",
                baseline_publication_id=None,
                baseline_version=None,
                proposed_patch={"operation": "publish_document", "title": "Employee Handbook"},
                proposed_namespace="company/policies",
                proposed_sensitivity="PL2_pii",
                source_refs=(f"company-evidence://{processed.evidence_id}",),
                source_coverage={
                    "complete": True,
                    "total_units": 1,
                    "covered_units": 1,
                    "missing_units": [],
                },
                conflict_candidates=(),
                ontology_mapping={},
                risk_level="normal",
                required_review_policy={
                    "minimum_approvals": 1,
                    "required_roles": ["org_admin"],
                    "separation": False,
                },
                idempotency_key="proposal:employee-handbook:v1",
                trace_id="trace-proposal",
            ),
        )
        submitted = await service.submit_proposal(
            db,
            principal=principal,
            proposal_id=proposal.id,
            expected_state_version=proposal.state_version,
            trace_id="trace-submit",
        )
        reviewed = await service.record_review(
            db,
            principal=principal,
            proposal_id=proposal.id,
            request=CompanyKnowledgeReviewRequest(
                decision="approve",
                reviewer_role="org_admin",
                reason="Evidence, ACL, validity, and policy text were reviewed.",
                evidence_refs=(f"company-evidence://{processed.evidence_id}",),
                policy_snapshot={"policy": "single-admin-normal-risk-v1"},
            ),
            expected_state_version=submitted.state_version,
            trace_id="trace-review",
        )
        assert reviewed.status == "approved"
        publication = await service.publish_proposal(
            db,
            principal=principal,
            proposal_id=proposal.id,
            expected_state_version=reviewed.state_version,
            valid_from=now,
            valid_until=None,
            trace_id="trace-publish",
        )
        await db.commit()

        assert publication.version == 1
        assert publication.status == "active"
        assert (
            await db.scalar(
                select(func.count(CompanyKnowledgeOutbox.id)).where(
                    CompanyKnowledgeOutbox.tenant_id == tenant_id,
                    CompanyKnowledgeOutbox.status == "pending",
                )
            )
            == 2
        )

    indexer = CompanyKnowledgeIndexer()
    summary = await indexer.process_pending(
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
        limit=20,
    )
    assert summary.completed == 2
    assert summary.failed == 0

    async with owner_sessionmaker() as db:
        document = await db.get(KnowledgeDocument, publication.document_id)
        segments = (
            (
                await db.execute(
                    select(KnowledgeSegment)
                    .where(KnowledgeSegment.document_id == publication.document_id)
                    .order_by(KnowledgeSegment.position)
                )
            )
            .scalars()
            .all()
        )
        assert document is not None
        assert document.scope_type == "company"
        assert document.scope_id == tenant_id
        assert segments and all(segment.scope_type == "company" for segment in segments)

        retired = await service.retire_publication(
            db,
            principal=principal,
            publication_id=publication.id,
            reason="Policy is temporarily withdrawn.",
            trace_id="trace-retire",
        )
        restored = await service.restore_publication(
            db,
            principal=principal,
            publication_id=retired.id,
            reason="Policy was re-approved without content changes.",
            valid_from=now,
            trace_id="trace-restore",
        )
        await db.commit()

        assert retired.status == "retired"
        assert restored.id != retired.id
        assert restored.version == 2
        assert restored.status == "active"
        assert restored.restored_from_publication_id == retired.id
        active_count = await db.scalar(
            select(func.count(CompanyKnowledgePublication.id)).where(
                CompanyKnowledgePublication.tenant_id == tenant_id,
                CompanyKnowledgePublication.logical_resource_key == restored.logical_resource_key,
                CompanyKnowledgePublication.status == "active",
            )
        )
        assert active_count == 1

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
            "company_knowledge.source_registered",
            "company_knowledge.ingest_completed",
            "company_knowledge.proposal_created",
            "company_knowledge.proposal_submitted",
            "company_knowledge.review_recorded",
            "company_knowledge.published",
            "company_knowledge.retired",
            "company_knowledge.restored",
        } <= {event.event_type for event in events}
        assert (
            await db.scalar(
                select(func.count(CompanyKnowledgeImportJob.id)).where(CompanyKnowledgeImportJob.tenant_id == tenant_id)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_company_import_hash_failure_is_durable_and_daemon_recovery_reenters_canonical_path(
    owner_sessionmaker,
    tmp_path: Path,
) -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    service = CompanyKnowledgeService(data_root=tmp_path)
    principal = _principal(tenant_id=tenant_id, user_id=user_id)
    markdown = "# Recovery\n\nThe canonical artifact must survive retry."

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Company KB Recovery", slug=f"ckb-recovery-{tenant_id.hex[:10]}"))
        db.add(
            User(
                id=user_id,
                username=f"ckb-recovery-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@ckb-recovery.test",
                password_hash="x",
                display_name="Company Knowledge Recovery Admin",
                role="org_admin",
                tenant_id=tenant_id,
            )
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        contract = await service.register_source_contract(
            db,
            principal=principal,
            contract_input=_contract(),
            idempotency_key="contract:recovery:v1",
            trace_id="trace-recovery-contract",
        )
        job = await service.queue_evidence_import(
            db,
            principal=principal,
            request=CompanyEvidenceIngestRequest(
                source_contract_id=contract.id,
                source_contract_version=1,
                evidence_kind="document",
                source_item_id="recovery-document",
                source_revision="v1",
                title="Recovery Document",
                markdown=markdown,
                typed_payload=None,
                external_artifact_ref=None,
                schema_ref="schema://company-document/v1",
                source_acl_snapshot={"role_names": ["org_admin"]},
                proposed_namespace="company/policies",
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
                purpose="verify import recovery",
                idempotency_key="import:recovery:v1",
                trace_id="trace-recovery-import",
            ),
        )
        await db.commit()

    artifact_path = Path(str(job.artifact_ref))
    artifact_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact_hash_mismatch"):
        await service.process_import_job(
            tenant_id=tenant_id,
            job_id=job.id,
            session_factory=owner_sessionmaker,
        )

    async with owner_sessionmaker() as db:
        failed = await db.get(CompanyKnowledgeImportJob, job.id)
        assert failed is not None
        assert failed.status == "queued"
        assert failed.attempt_count == 1
        assert failed.last_error_code == "ValueError"
        assert failed.last_error == "company_knowledge_import_artifact_hash_mismatch"
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
        failed.available_at = now
        await db.commit()

    artifact_path.write_text(f"{markdown}\n", encoding="utf-8")
    async with owner_sessionmaker() as db:
        summary = await service.recover_due_import_jobs(
            db,
            session_factory=owner_sessionmaker,
            limit=10,
        )
    assert summary.attempted == 1
    assert summary.completed == 1
    assert summary.failed == 0

    async with owner_sessionmaker() as db:
        recovered = await db.get(CompanyKnowledgeImportJob, job.id)
        assert recovered is not None
        assert recovered.status == "completed"
        assert recovered.attempt_count == 2
        assert recovered.document_id is not None
        assert recovered.evidence_id is not None
