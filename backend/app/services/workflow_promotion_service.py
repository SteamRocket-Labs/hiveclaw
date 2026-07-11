"""Immutable, two-person promotion of completed Workflow runs.

The session owner proposes the exact archived definition plus a deterministic
run-evidence snapshot. A different human with Agent manage authority reviews
that proposal through the API. Approval, active Workflow creation, AI-asset
projection, and audit are committed in one tenant transaction.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import tenant_scoped_session
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.models.workflow import (
    WorkflowDefinitionRecord,
    WorkflowLeafCall,
    WorkflowPromotionProposal,
    WorkflowQuota,
    WorkflowStep,
)
from app.runtime.workflow_compiler import WorkflowCompileError, compile_workflow
from app.runtime.workflow_definition import compute_definition_hash


class WorkflowPromotionError(ValueError):
    """Base class for governed promotion failures."""


class WorkflowPromotionNotFound(WorkflowPromotionError):
    """Run/proposal is outside the requested tenant+Agent boundary."""


class WorkflowPromotionForbidden(WorkflowPromotionError):
    """The principal is not the run's initiating session owner."""


class WorkflowPromotionConflict(WorkflowPromotionError):
    """The requested lifecycle transition violates promotion invariants."""


class WorkflowPromotionStaleError(WorkflowPromotionConflict):
    """The source evidence changed after proposal submission."""


@dataclass(slots=True)
class WorkflowPromotionReviewResult:
    proposal: WorkflowPromotionProposal
    definition: WorkflowDefinitionRecord | None


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _value_hash(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


class WorkflowPromotionService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory

    def _session(self, tenant_id: uuid.UUID):
        return tenant_scoped_session(str(tenant_id), session_factory=self._session_factory, require_tenant=True)

    @staticmethod
    async def _load_source(
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        run_id: uuid.UUID,
        for_update: bool,
    ) -> tuple[RuntimeTask, ChatSession, dict, str, dict]:
        query = select(RuntimeTask).where(
            RuntimeTask.id == run_id,
            RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.task_type == "workflow",
        )
        if for_update:
            query = query.with_for_update()
        run = (await db.execute(query)).scalar_one_or_none()
        if run is None:
            raise WorkflowPromotionNotFound("workflow run not found")
        if run.status != "completed":
            raise WorkflowPromotionConflict("only completed workflow runs can be proposed")

        metadata = dict(run.metadata_json or {})
        if metadata.get("tenant_id") != str(tenant_id):
            raise WorkflowPromotionNotFound("workflow run not found")
        if metadata.get("definition_source") not in {"ephemeral", "dynamic_workflow"}:
            raise WorkflowPromotionConflict("only ephemeral or dynamic Workflow runs can be promoted")
        definition = metadata.get("definition_json")
        if not isinstance(definition, dict) or not definition:
            raise WorkflowPromotionConflict("workflow run has no archived definition")
        definition_hash = compute_definition_hash(definition)
        if metadata.get("definition_hash") != definition_hash:
            raise WorkflowPromotionConflict("archived Workflow definition hash does not match the run")

        session_value = run.root_session_id or run.parent_session_id
        try:
            root_session_id = uuid.UUID(str(session_value or ""))
        except ValueError as exc:
            raise WorkflowPromotionConflict("workflow run has no valid root session authority") from exc
        source_session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == root_session_id,
                    ChatSession.tenant_id == tenant_id,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if source_session is None:
            raise WorkflowPromotionConflict("workflow run root session authority cannot be recovered")
        return run, source_session, definition, definition_hash, metadata

    @staticmethod
    async def _evidence_snapshot(
        db: AsyncSession,
        *,
        run: RuntimeTask,
        metadata: dict,
        definition_hash: str,
    ) -> dict:
        steps = (
            (
                await db.execute(
                    select(WorkflowStep)
                    .where(WorkflowStep.run_id == run.id, WorkflowStep.tenant_id == run.tenant_id)
                    .order_by(WorkflowStep.step_id)
                )
            )
            .scalars()
            .all()
        )
        leaves = (
            (
                await db.execute(
                    select(WorkflowLeafCall)
                    .where(WorkflowLeafCall.run_id == run.id, WorkflowLeafCall.tenant_id == run.tenant_id)
                    .order_by(WorkflowLeafCall.step_id, WorkflowLeafCall.leaf_id)
                )
            )
            .scalars()
            .all()
        )
        quota = (
            await db.execute(
                select(WorkflowQuota).where(WorkflowQuota.run_id == run.id, WorkflowQuota.tenant_id == run.tenant_id)
            )
        ).scalar_one_or_none()
        return {
            "schema": "hive.workflow_promotion_evidence.v1",
            "run_id": str(run.id),
            "status": run.status,
            "definition_hash": definition_hash,
            "definition_source": metadata.get("definition_source"),
            "args_hash": metadata.get("args_hash"),
            "config_snapshot_hash": run.config_snapshot_hash,
            "policy_snapshot_hash": run.policy_snapshot_hash,
            "result_summary_hash": _value_hash(run.result_summary),
            "metadata_hash": _canonical_hash(metadata),
            "created_at": _iso(run.created_at),
            "completed_at": _iso(run.completed_at),
            "outcome": dict(metadata.get("outcome_evidence") or {}),
            "steps": [
                {
                    "step_id": row.step_id,
                    "step_type": row.step_type,
                    "status": row.status,
                    "input_hash": row.input_hash,
                    "definition_hash": row.definition_hash,
                    "result_ref_hash": _value_hash(row.result_ref),
                    "error_hash": _value_hash(row.error),
                    "started_at": _iso(row.started_at),
                    "finished_at": _iso(row.finished_at),
                }
                for row in steps
            ],
            "leaves": [
                {
                    "step_id": row.step_id,
                    "leaf_id": row.leaf_id,
                    "status": row.status,
                    "input_hash": row.input_hash,
                    "definition_hash": row.definition_hash,
                    "idempotency_key": row.idempotency_key,
                    "result_ref_hash": _value_hash(row.result_ref),
                    "token_usage": dict(row.token_usage or {}),
                    "error_hash": _value_hash(row.error),
                    "started_at": _iso(row.started_at),
                    "finished_at": _iso(row.finished_at),
                }
                for row in leaves
            ],
            "quota": (
                {
                    "allocated_tokens": int(quota.allocated_tokens or 0),
                    "consumed_tokens": int(quota.consumed_tokens or 0),
                }
                if quota is not None
                else None
            ),
        }

    async def submit(
        self,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        run_id: uuid.UUID,
        requester_user_id: uuid.UUID,
    ) -> WorkflowPromotionProposal:
        proposal_hash: str | None = None
        try:
            async with self._session(tenant_id) as db:
                run, source_session, definition, definition_hash, metadata = await self._load_source(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    run_id=run_id,
                    for_update=True,
                )
                if source_session.user_id != requester_user_id or (
                    run.root_user_id is not None and run.root_user_id != requester_user_id
                ):
                    raise WorkflowPromotionForbidden("only the initiating session owner can submit this proposal")
                evidence = await self._evidence_snapshot(
                    db,
                    run=run,
                    metadata=metadata,
                    definition_hash=definition_hash,
                )
                evidence_hash = _canonical_hash(evidence)
                proposal_hash = _canonical_hash(
                    {
                        "tenant_id": str(tenant_id),
                        "agent_id": str(agent_id),
                        "run_id": str(run_id),
                        "root_session_id": str(source_session.id),
                        "requester_user_id": str(requester_user_id),
                        "definition_hash": definition_hash,
                        "run_evidence_hash": evidence_hash,
                    }
                )
                existing = (
                    await db.execute(
                        select(WorkflowPromotionProposal)
                        .where(
                            WorkflowPromotionProposal.tenant_id == tenant_id,
                            WorkflowPromotionProposal.proposal_hash == proposal_hash,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.status == "withdrawn":
                        existing.status = "pending"
                        existing.reviewer_user_id = None
                        existing.review_reason = None
                        existing.reviewed_at = None
                        db.add(
                            AuditLog(
                                tenant_id=tenant_id,
                                agent_id=agent_id,
                                user_id=requester_user_id,
                                action="workflow_promotion.resubmitted",
                                details={
                                    "proposal_id": str(existing.id),
                                    "run_id": str(run_id),
                                },
                            )
                        )
                        await db.flush()
                        await db.refresh(existing)
                    return existing

                proposal = WorkflowPromotionProposal(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    run_id=run_id,
                    root_session_id=source_session.id,
                    requester_user_id=requester_user_id,
                    proposal_hash=proposal_hash,
                    run_evidence_hash=evidence_hash,
                    definition_hash=definition_hash,
                    definition_json=definition,
                    run_evidence_json=evidence,
                    status="pending",
                )
                db.add(proposal)
                await db.flush()
                db.add(
                    AuditLog(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        user_id=requester_user_id,
                        action="workflow_promotion.submitted",
                        details={
                            "proposal_id": str(proposal.id),
                            "run_id": str(run_id),
                            "definition_hash": definition_hash,
                            "run_evidence_hash": evidence_hash,
                        },
                    )
                )
                await db.flush()
                await db.refresh(proposal)
                return proposal
        except IntegrityError as exc:
            # A concurrent replay can lose the unique proposal_hash race. The
            # immutable winner is the idempotent response.
            cause = getattr(exc.orig, "__cause__", None)
            constraint_name = getattr(cause, "constraint_name", None) or getattr(exc.orig, "constraint_name", None)
            if constraint_name != "uq_workflow_promotion_proposal_hash" or proposal_hash is None:
                raise
            async with self._session(tenant_id) as db:
                replay = (
                    await db.execute(
                        select(WorkflowPromotionProposal).where(
                            WorkflowPromotionProposal.tenant_id == tenant_id,
                            WorkflowPromotionProposal.proposal_hash == proposal_hash,
                        )
                    )
                ).scalar_one_or_none()
                if replay is None:
                    raise
                return replay

    async def review(
        self,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        proposal_id: uuid.UUID,
        reviewer_user_id: uuid.UUID,
        decision: Literal["approve", "reject"],
        reason: str | None = None,
    ) -> WorkflowPromotionReviewResult:
        normalized_reason = str(reason or "").strip()
        stale = False
        result: WorkflowPromotionReviewResult | None = None
        async with self._session(tenant_id) as db:
            proposal = (
                await db.execute(
                    select(WorkflowPromotionProposal)
                    .where(
                        WorkflowPromotionProposal.id == proposal_id,
                        WorkflowPromotionProposal.tenant_id == tenant_id,
                        WorkflowPromotionProposal.agent_id == agent_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if proposal is None:
                raise WorkflowPromotionNotFound("workflow promotion proposal not found")
            reviewer = (
                await db.execute(select(User.id).where(User.id == reviewer_user_id, User.tenant_id == tenant_id))
            ).scalar_one_or_none()
            if reviewer is None:
                raise WorkflowPromotionForbidden("promotion reviewer does not belong to the proposal tenant")
            if proposal.requester_user_id == reviewer_user_id:
                raise WorkflowPromotionConflict("promotion review requires a different human reviewer")

            existing_definition = (
                await db.execute(
                    select(WorkflowDefinitionRecord).where(
                        WorkflowDefinitionRecord.promotion_proposal_id == proposal.id
                    )
                )
            ).scalar_one_or_none()
            if proposal.status == "approved" and decision == "approve" and existing_definition is not None:
                return WorkflowPromotionReviewResult(proposal=proposal, definition=existing_definition)
            if proposal.status == "rejected" and decision == "reject":
                return WorkflowPromotionReviewResult(proposal=proposal, definition=None)
            if proposal.status != "pending":
                raise WorkflowPromotionConflict(f"proposal is already {proposal.status}")

            run, source_session, current_definition, current_definition_hash, metadata = await self._load_source(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                run_id=proposal.run_id,
                for_update=True,
            )
            evidence = await self._evidence_snapshot(
                db,
                run=run,
                metadata=metadata,
                definition_hash=current_definition_hash,
            )
            if (
                source_session.id != proposal.root_session_id
                or current_definition_hash != proposal.definition_hash
                or current_definition != proposal.definition_json
                or _canonical_hash(evidence) != proposal.run_evidence_hash
            ):
                proposal.status = "stale"
                proposal.reviewer_user_id = reviewer_user_id
                proposal.review_reason = "Source run evidence changed after submission"
                proposal.reviewed_at = datetime.now(UTC)
                db.add(
                    AuditLog(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        user_id=reviewer_user_id,
                        action="workflow_promotion.stale",
                        details={"proposal_id": str(proposal.id), "run_id": str(proposal.run_id)},
                    )
                )
                await db.flush()
                stale = True
                result = WorkflowPromotionReviewResult(proposal=proposal, definition=None)
            elif decision == "reject":
                if not normalized_reason:
                    raise WorkflowPromotionConflict("rejection requires a review reason")
                proposal.status = "rejected"
                proposal.reviewer_user_id = reviewer_user_id
                proposal.review_reason = normalized_reason
                proposal.reviewed_at = datetime.now(UTC)
                db.add(
                    AuditLog(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        user_id=reviewer_user_id,
                        action="workflow_promotion.rejected",
                        details={
                            "proposal_id": str(proposal.id),
                            "run_id": str(proposal.run_id),
                            "reason": normalized_reason,
                        },
                    )
                )
                await db.flush()
                result = WorkflowPromotionReviewResult(proposal=proposal, definition=None)
            else:
                try:
                    compiled = compile_workflow(proposal.definition_json)
                except WorkflowCompileError as exc:
                    raise WorkflowPromotionConflict(f"promotion compile check failed: {exc}") from exc

                await db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"workflow-version:{tenant_id}:{compiled.definition.name}"},
                )
                current_max = (
                    await db.execute(
                        select(func.max(WorkflowDefinitionRecord.definition_version)).where(
                            WorkflowDefinitionRecord.tenant_id == tenant_id,
                            WorkflowDefinitionRecord.name == compiled.definition.name,
                        )
                    )
                ).scalar_one_or_none()
                definition = existing_definition or WorkflowDefinitionRecord(
                    tenant_id=tenant_id,
                    name=compiled.definition.name,
                    definition_version=int(current_max or 0) + 1,
                    definition_hash=proposal.definition_hash,
                    definition_json=proposal.definition_json,
                    status="active",
                    visibility_scope="agent",
                    owner_type="agent",
                    owner_id=agent_id,
                    created_by_user_id=proposal.requester_user_id,
                    created_by_agent_id=agent_id,
                    promoted_from_run_id=proposal.run_id,
                    promotion_proposal_id=proposal.id,
                )
                if existing_definition is None:
                    db.add(definition)
                    await db.flush()

                from app.services.ai_asset_adapters import project_workflow
                from app.services.ai_assets import register_projection

                await register_projection(
                    db,
                    project_workflow(definition),
                    change_source="publish",
                    actor_user_id=reviewer_user_id,
                    actor_agent_id=agent_id,
                    change_message=f"Approved Workflow promotion proposal {proposal.id}",
                )
                proposal.status = "approved"
                proposal.reviewer_user_id = reviewer_user_id
                proposal.review_reason = normalized_reason or "Approved"
                proposal.reviewed_at = datetime.now(UTC)
                db.add(
                    AuditLog(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        user_id=reviewer_user_id,
                        action="workflow_promotion.approved",
                        details={
                            "proposal_id": str(proposal.id),
                            "run_id": str(proposal.run_id),
                            "definition_id": str(definition.id),
                            "requester_user_id": str(proposal.requester_user_id),
                            "reviewer_user_id": str(reviewer_user_id),
                        },
                    )
                )
                await db.flush()
                await db.refresh(proposal)
                await db.refresh(definition)
                result = WorkflowPromotionReviewResult(proposal=proposal, definition=definition)

        if stale:
            raise WorkflowPromotionStaleError("source run evidence changed; proposal marked stale")
        if result is None:  # pragma: no cover - defensive invariant
            raise WorkflowPromotionConflict("promotion review produced no result")
        return result

    async def withdraw(
        self,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        proposal_id: uuid.UUID,
        requester_user_id: uuid.UUID,
    ) -> WorkflowPromotionProposal:
        async with self._session(tenant_id) as db:
            proposal = (
                await db.execute(
                    select(WorkflowPromotionProposal)
                    .where(
                        WorkflowPromotionProposal.id == proposal_id,
                        WorkflowPromotionProposal.tenant_id == tenant_id,
                        WorkflowPromotionProposal.agent_id == agent_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if proposal is None:
                raise WorkflowPromotionNotFound("workflow promotion proposal not found")
            if proposal.requester_user_id != requester_user_id:
                raise WorkflowPromotionForbidden("only the requester can withdraw this proposal")
            if proposal.status == "withdrawn":
                return proposal
            if proposal.status != "pending":
                raise WorkflowPromotionConflict(f"proposal is already {proposal.status}")
            proposal.status = "withdrawn"
            proposal.review_reason = "Withdrawn by requester"
            proposal.reviewed_at = datetime.now(UTC)
            db.add(
                AuditLog(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    user_id=requester_user_id,
                    action="workflow_promotion.withdrawn",
                    details={"proposal_id": str(proposal.id), "run_id": str(proposal.run_id)},
                )
            )
            await db.flush()
            await db.refresh(proposal)
            return proposal

    async def list_proposals(
        self,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        requester_user_id: uuid.UUID,
        include_all: bool,
    ) -> list[tuple[WorkflowPromotionProposal, WorkflowDefinitionRecord | None]]:
        async with self._session(tenant_id) as db:
            query = select(WorkflowPromotionProposal).where(
                WorkflowPromotionProposal.tenant_id == tenant_id,
                WorkflowPromotionProposal.agent_id == agent_id,
            )
            if not include_all:
                query = query.where(WorkflowPromotionProposal.requester_user_id == requester_user_id)
            proposals = (await db.execute(query.order_by(WorkflowPromotionProposal.created_at.desc()))).scalars().all()
            ids = [proposal.id for proposal in proposals]
            definitions: dict[uuid.UUID, WorkflowDefinitionRecord] = {}
            if ids:
                rows = (
                    (
                        await db.execute(
                            select(WorkflowDefinitionRecord).where(
                                WorkflowDefinitionRecord.promotion_proposal_id.in_(ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                definitions = {row.promotion_proposal_id: row for row in rows if row.promotion_proposal_id}
            return [(proposal, definitions.get(proposal.id)) for proposal in proposals]
