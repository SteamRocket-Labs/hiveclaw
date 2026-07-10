"""Real PostgreSQL proof for Personal KB proposal authority, revision, and rollback."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.config_revision import ConfigRevision
from app.models.knowledge import KnowledgeDocument, PersonalKnowledgeProposal
from app.models.tenant import Tenant
from app.models.user import User
from app.services.personal_knowledge_proposals import (
    PersonalKnowledgeProposalRejected,
    PersonalKnowledgeProposalService,
)
from app.services.personal_knowledge_service import PersonalKnowledgeService


async def _seed(owner_sessionmaker):
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    foreign_owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    foreign_agent_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="Personal KB Tenant", slug=f"personal-kb-{suffix}"))
        db.add_all(
            [
                User(
                    id=owner_id,
                    username=f"pkb-owner-{suffix}",
                    email=f"pkb-owner-{suffix}@example.test",
                    password_hash="x",
                    display_name="Personal KB Owner",
                    tenant_id=tenant_id,
                    role="member",
                ),
                User(
                    id=foreign_owner_id,
                    username=f"pkb-foreign-{suffix}",
                    email=f"pkb-foreign-{suffix}@example.test",
                    password_hash="x",
                    display_name="Foreign Owner",
                    tenant_id=tenant_id,
                    role="member",
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    creator_id=owner_id,
                    owner_user_id=owner_id,
                    name="Owner Research Agent",
                    role_description="Proposes durable owner knowledge",
                    status="idle",
                ),
                Agent(
                    id=foreign_agent_id,
                    tenant_id=tenant_id,
                    creator_id=foreign_owner_id,
                    owner_user_id=foreign_owner_id,
                    name="Foreign Agent",
                    role_description="Must not write another owner scope",
                    status="idle",
                ),
            ]
        )
    return tenant_id, owner_id, agent_id, foreign_agent_id


async def test_proposal_requires_owner_review_then_commits_revision_and_rolls_back(
    owner_sessionmaker,
    tmp_path,
) -> None:
    tenant_id, owner_id, agent_id, _ = await _seed(owner_sessionmaker)
    service = PersonalKnowledgeProposalService(
        data_root=tmp_path,
        knowledge_service=PersonalKnowledgeService(data_root=tmp_path, extractor=None),
    )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        first = await service.propose(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            proposed_by_agent_id=agent_id,
            title="Release runbook",
            content="# Release\n\nUse a canary first.",
            target_collection="operations",
            sensitivity="internal",
            source_refs=["session://11111111-1111-1111-1111-111111111111"],
            purpose="Preserve the owner's deployment procedure.",
            dedupe_key="release-runbook",
            idempotency_key="proposal:first",
        )
        assert first.policy_outcome == "ask"
        assert first.status == "pending"

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        committed_first = await service.review(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            proposal_id=first.proposal_id,
            reviewer_user_id=owner_id,
            decision="approve",
            reason="Owner verified the source.",
        )
        assert committed_first.status == "committed"
        assert committed_first.document_id is not None
        assert committed_first.revision_id is not None
        document_id = committed_first.document_id

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        second = await service.propose(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            proposed_by_agent_id=agent_id,
            title="Release runbook",
            content="# Release\n\nUse a canary first, then verify rollback telemetry.",
            target_collection="operations",
            sensitivity="internal",
            source_refs=["session://22222222-2222-2222-2222-222222222222"],
            purpose="Update the owner's deployment procedure.",
            dedupe_key="release-runbook",
            idempotency_key="proposal:second",
        )
        assert second.baseline_document_id == document_id
        assert second.baseline_revision_id == committed_first.revision_id
        assert second.baseline_content_hash is not None
        assert "-Use a canary first." in second.diff_unified
        assert "+Use a canary first, then verify rollback telemetry." in second.diff_unified
        committed_second = await service.review(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            proposal_id=second.proposal_id,
            reviewer_user_id=owner_id,
            decision="approve",
        )
        assert committed_second.document_id == document_id

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        rollback = await service.rollback_document(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            document_id=document_id,
            target_version=1,
            reviewer_user_id=owner_id,
        )
        assert rollback.version == 3
        assert rollback.rollback_of_version == 1

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        document = await db.get(KnowledgeDocument, document_id)
        revisions = (
            (
                await db.execute(
                    select(ConfigRevision)
                    .where(
                        ConfigRevision.entity_type == "personal_knowledge_document",
                        ConfigRevision.entity_id == document_id,
                    )
                    .order_by(ConfigRevision.version)
                )
            )
            .scalars()
            .all()
        )
        audits = (
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action.in_(("personal_kb.proposal.commit", "personal_kb.document.rollback")),
                    )
                )
            )
            .scalars()
            .all()
        )

    assert document is not None
    assert document.canonical_md_sha256 == revisions[0].content["canonical_md_sha256"]
    assert [revision.version for revision in revisions] == [1, 2, 3]
    assert revisions[-1].rollback_of_revision_id == revisions[0].id
    assert len(audits) == 3


async def test_cross_owner_agent_is_rejected_without_storing_candidate_content(
    owner_sessionmaker,
    tmp_path,
) -> None:
    tenant_id, owner_id, _, foreign_agent_id = await _seed(owner_sessionmaker)
    service = PersonalKnowledgeProposalService(
        data_root=tmp_path,
        knowledge_service=PersonalKnowledgeService(data_root=tmp_path, extractor=None),
    )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        with pytest.raises(PersonalKnowledgeProposalRejected, match="agent_owner_mismatch"):
            await service.propose(
                db,
                tenant_id=tenant_id,
                owner_user_id=owner_id,
                proposed_by_agent_id=foreign_agent_id,
                title="Unauthorized",
                content="This content must not enter the owner's proposal queue.",
                target_collection="inbox",
                sensitivity="internal",
                source_refs=["session://33333333-3333-3333-3333-333333333333"],
                purpose="Unauthorized cross-owner attempt.",
                dedupe_key="unauthorized",
                idempotency_key="proposal:cross-owner",
            )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        proposals = (
            (
                await db.execute(
                    select(PersonalKnowledgeProposal).where(
                        PersonalKnowledgeProposal.tenant_id == tenant_id,
                        PersonalKnowledgeProposal.owner_user_id == owner_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        audit = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.action == "personal_kb.proposal.rejected_authority",
                )
            )
        ).scalar_one()

    assert proposals == []
    assert audit.details["reason"] == "agent_owner_mismatch"
    assert "content" not in audit.details
