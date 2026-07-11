from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.models.workflow import WorkflowDefinitionRecord, WorkflowPromotionProposal
from app.runtime.workflow_definition import compute_definition_hash
from app.services.workflow_definitions import WorkflowDefinitionError, WorkflowDefinitionService
from app.services.workflow_promotion_service import (
    WorkflowPromotionConflict,
    WorkflowPromotionService,
    WorkflowPromotionStaleError,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _definition(name: str = "approved-from-run") -> dict:
    return {
        "name": name,
        "description": "Immutable run evidence",
        "args_schema": {},
        "steps": [
            {
                "id": "collect",
                "type": "agent_step",
                "leaf": {"name": "collector", "type": "worker"},
                "task": "Collect evidence",
            }
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tenant_id = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tenant_id, name="promotion", slug=f"promotion-{tenant_id.hex[:8]}"))
    return tenant_id


@pytest.fixture()
def service(owner_sessionmaker) -> WorkflowPromotionService:
    return WorkflowPromotionService(session_factory=owner_sessionmaker)


@pytest.fixture()
async def reviewer_user_id(owner_sessionmaker, tenant_id) -> uuid.UUID:
    reviewer_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=reviewer_id,
                username=f"reviewer-{reviewer_id.hex[:8]}",
                email=f"reviewer-{reviewer_id.hex[:8]}@test.local",
                password_hash="x",
                display_name="Independent Reviewer",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
    return reviewer_id


async def _seed_completed_run(
    owner_sessionmaker,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    requester_user_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    definition = _definition()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=requester_user_id,
                title="Promotion source",
                source_channel="web",
            )
        )
        session.add(
            RuntimeTask(
                id=run_id,
                tenant_id=tenant_id,
                task_type="workflow",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_session_id=str(session_id),
                root_user_id=requester_user_id,
                status="completed",
                result_summary="completed with evidence",
                completed_at=datetime.now(UTC),
                metadata_json={
                    "tenant_id": str(tenant_id),
                    "definition_source": "ephemeral",
                    "definition_json": definition,
                    "definition_hash": compute_definition_hash(definition),
                    "args_hash": "args-v1",
                    "outcome_evidence": {"leaf_total": 1, "leaf_done": 1, "leaf_failed": 0},
                },
            )
        )
    return session_id, run_id


async def test_owner_submission_is_idempotent_and_does_not_require_manager(
    service,
    owner_sessionmaker,
    tenant_id,
    workflow_principals,
):
    _session_id, run_id = await _seed_completed_run(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        requester_user_id=workflow_principals.user_id,
    )

    first = await service.submit(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        run_id=run_id,
        requester_user_id=workflow_principals.user_id,
    )
    replay = await service.submit(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        run_id=run_id,
        requester_user_id=workflow_principals.user_id,
    )

    assert first.id == replay.id
    assert first.status == "pending"
    assert first.run_evidence_hash
    assert first.definition_hash
    assert first.run_evidence_json["status"] == "completed"


async def test_proposal_snapshot_cannot_be_rewritten_or_deleted(
    service,
    owner_sessionmaker,
    tenant_id,
    workflow_principals,
):
    _session_id, run_id = await _seed_completed_run(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        requester_user_id=workflow_principals.user_id,
    )
    proposal = await service.submit(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        run_id=run_id,
        requester_user_id=workflow_principals.user_id,
    )

    with pytest.raises(Exception, match="snapshots are immutable"):
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            persisted = await session.get(WorkflowPromotionProposal, proposal.id)
            persisted.definition_json = {"name": "rewritten"}

    with pytest.raises(Exception, match="snapshots are immutable"):
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            persisted = await session.get(WorkflowPromotionProposal, proposal.id)
            await session.delete(persisted)


async def test_independent_manager_approval_atomically_creates_active_asset_and_audit(
    service,
    owner_sessionmaker,
    tenant_id,
    workflow_principals,
    reviewer_user_id,
):
    _session_id, run_id = await _seed_completed_run(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        requester_user_id=workflow_principals.user_id,
    )
    proposal = await service.submit(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        run_id=run_id,
        requester_user_id=workflow_principals.user_id,
    )

    reviewed = await service.review(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        proposal_id=proposal.id,
        reviewer_user_id=reviewer_user_id,
        decision="approve",
        reason="Evidence and scope verified",
    )
    replay = await service.review(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        proposal_id=proposal.id,
        reviewer_user_id=reviewer_user_id,
        decision="approve",
        reason="Evidence and scope verified",
    )

    assert reviewed.proposal.status == "approved"
    assert reviewed.definition is not None
    assert reviewed.definition.status == "active"
    assert reviewed.definition.promotion_proposal_id == proposal.id
    assert replay.definition.id == reviewed.definition.id

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        definitions = (
            (
                await session.execute(
                    select(WorkflowDefinitionRecord).where(
                        WorkflowDefinitionRecord.promotion_proposal_id == proposal.id
                    )
                )
            )
            .scalars()
            .all()
        )
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action == "workflow_promotion.approved",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(definitions) == 1
    assert len(audits) == 1


async def test_requester_cannot_review_own_proposal(
    service,
    owner_sessionmaker,
    tenant_id,
    workflow_principals,
):
    _session_id, run_id = await _seed_completed_run(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        requester_user_id=workflow_principals.user_id,
    )
    proposal = await service.submit(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        run_id=run_id,
        requester_user_id=workflow_principals.user_id,
    )

    with pytest.raises(WorkflowPromotionConflict, match="different human"):
        await service.review(
            tenant_id=tenant_id,
            agent_id=workflow_principals.agent_id,
            proposal_id=proposal.id,
            reviewer_user_id=workflow_principals.user_id,
            decision="approve",
        )


async def test_reviewer_identity_must_belong_to_the_proposal_tenant(
    service,
    owner_sessionmaker,
    tenant_id,
    workflow_principals,
):
    from app.models.tenant import Tenant
    from app.services.workflow_promotion_service import WorkflowPromotionForbidden

    _session_id, run_id = await _seed_completed_run(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        requester_user_id=workflow_principals.user_id,
    )
    proposal = await service.submit(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        run_id=run_id,
        requester_user_id=workflow_principals.user_id,
    )
    foreign_tenant_id = uuid.uuid4()
    foreign_user_id = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(
            Tenant(
                id=foreign_tenant_id,
                name="foreign reviewer",
                slug=f"foreign-reviewer-{foreign_tenant_id.hex[:8]}",
            )
        )
        await session.flush()
        session.add(
            User(
                id=foreign_user_id,
                username=f"foreign-{foreign_user_id.hex[:8]}",
                email=f"foreign-{foreign_user_id.hex[:8]}@test.local",
                password_hash="x",
                display_name="Foreign Reviewer",
                tenant_id=foreign_tenant_id,
                role="org_admin",
            )
        )

    with pytest.raises(WorkflowPromotionForbidden, match="tenant"):
        await service.review(
            tenant_id=tenant_id,
            agent_id=workflow_principals.agent_id,
            proposal_id=proposal.id,
            reviewer_user_id=foreign_user_id,
            decision="approve",
        )


async def test_withdraw_replay_and_resubmit_are_recoverable(
    service,
    owner_sessionmaker,
    tenant_id,
    workflow_principals,
):
    _session_id, run_id = await _seed_completed_run(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        requester_user_id=workflow_principals.user_id,
    )
    proposal = await service.submit(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        run_id=run_id,
        requester_user_id=workflow_principals.user_id,
    )
    withdrawn = await service.withdraw(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        proposal_id=proposal.id,
        requester_user_id=workflow_principals.user_id,
    )
    replay = await service.withdraw(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        proposal_id=proposal.id,
        requester_user_id=workflow_principals.user_id,
    )
    reopened = await service.submit(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        run_id=run_id,
        requester_user_id=workflow_principals.user_id,
    )

    assert withdrawn.status == "withdrawn"
    assert replay.id == proposal.id
    assert reopened.id == proposal.id
    assert reopened.status == "pending"
    assert reopened.reviewed_at is None
    assert reopened.review_reason is None


async def test_rejection_requires_reason_and_is_idempotent(
    service,
    owner_sessionmaker,
    tenant_id,
    workflow_principals,
    reviewer_user_id,
):
    _session_id, run_id = await _seed_completed_run(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        requester_user_id=workflow_principals.user_id,
    )
    proposal = await service.submit(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        run_id=run_id,
        requester_user_id=workflow_principals.user_id,
    )
    with pytest.raises(WorkflowPromotionConflict, match="reason"):
        await service.review(
            tenant_id=tenant_id,
            agent_id=workflow_principals.agent_id,
            proposal_id=proposal.id,
            reviewer_user_id=reviewer_user_id,
            decision="reject",
        )
    rejected = await service.review(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        proposal_id=proposal.id,
        reviewer_user_id=reviewer_user_id,
        decision="reject",
        reason="Insufficient outcome evidence",
    )
    replay = await service.review(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        proposal_id=proposal.id,
        reviewer_user_id=reviewer_user_id,
        decision="reject",
        reason="Insufficient outcome evidence",
    )
    assert rejected.proposal.status == "rejected"
    assert replay.proposal.id == proposal.id


async def test_evidence_drift_marks_proposal_stale_and_never_creates_definition(
    service,
    owner_sessionmaker,
    tenant_id,
    workflow_principals,
    reviewer_user_id,
):
    _session_id, run_id = await _seed_completed_run(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        requester_user_id=workflow_principals.user_id,
    )
    proposal = await service.submit(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        run_id=run_id,
        requester_user_id=workflow_principals.user_id,
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        run = await session.get(RuntimeTask, run_id)
        run.result_summary = "mutated after proposal"

    with pytest.raises(WorkflowPromotionStaleError):
        await service.review(
            tenant_id=tenant_id,
            agent_id=workflow_principals.agent_id,
            proposal_id=proposal.id,
            reviewer_user_id=reviewer_user_id,
            decision="approve",
        )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        persisted = await session.get(WorkflowPromotionProposal, proposal.id)
        definition = (
            await session.execute(
                select(WorkflowDefinitionRecord).where(WorkflowDefinitionRecord.promotion_proposal_id == proposal.id)
            )
        ).scalar_one_or_none()
    assert persisted.status == "stale"
    assert definition is None


async def test_legacy_direct_promotion_is_quarantined_from_activation_and_execution(
    owner_sessionmaker,
    tenant_id,
    workflow_principals,
):
    definitions = WorkflowDefinitionService(session_factory=owner_sessionmaker)
    legacy = await definitions.create_draft(
        tenant_id=tenant_id,
        definition_data=_definition("legacy-direct"),
        created_by_user_id=workflow_principals.user_id,
        owner_type="agent",
        owner_id=workflow_principals.agent_id,
        promoted_from_run_id=uuid.uuid4(),
    )

    with pytest.raises(WorkflowDefinitionError, match="promotion proposal"):
        await definitions.activate(
            legacy.id,
            tenant_id=tenant_id,
            actor_user_id=workflow_principals.user_id,
        )


async def test_pending_proposal_link_cannot_be_used_as_an_execution_bypass(
    service,
    owner_sessionmaker,
    tenant_id,
    workflow_principals,
):
    _session_id, run_id = await _seed_completed_run(
        owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        requester_user_id=workflow_principals.user_id,
    )
    proposal = await service.submit(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        run_id=run_id,
        requester_user_id=workflow_principals.user_id,
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            WorkflowDefinitionRecord(
                tenant_id=tenant_id,
                name="pending-bypass",
                definition_version=1,
                definition_hash=proposal.definition_hash,
                definition_json={**proposal.definition_json, "name": "pending-bypass"},
                status="active",
                visibility_scope="agent",
                owner_type="agent",
                owner_id=workflow_principals.agent_id,
                created_by_user_id=workflow_principals.user_id,
                promoted_from_run_id=run_id,
                promotion_proposal_id=proposal.id,
            )
        )

    definitions = WorkflowDefinitionService(session_factory=owner_sessionmaker)
    with pytest.raises(WorkflowDefinitionError, match="approved promotion proposal"):
        await definitions.resolve_for_execution(
            tenant_id=tenant_id,
            name="pending-bypass",
            agent_id=workflow_principals.agent_id,
        )
