"""Persistence and state transitions for canonical Workflow confirmation artifacts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_task import RuntimeTask
from app.models.workflow_confirmation import WorkflowPreviewArtifact, WorkflowProposalArtifact
from app.runtime.workflow_definition import compute_definition_hash

WORKFLOW_CONFIRMATION_TTL = timedelta(hours=24)
WORKFLOW_START_LEASE = timedelta(minutes=5)


class WorkflowConfirmationConflict(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WorkflowStartClaim:
    outcome: str
    preview: WorkflowPreviewArtifact
    run_exists: bool = False


def workflow_preview_artifact_payload(preview: WorkflowPreviewArtifact) -> dict[str, Any]:
    return {
        "preview_id": str(preview.id),
        "session_id": str(preview.session_id),
        "preview_status": preview.status,
        "artifact_version": preview.artifact_version,
        "artifact_hash": preview.artifact_hash,
        "proposal_id": str(preview.proposal_id) if preview.proposal_id else None,
        "candidate_id": preview.candidate_id,
        "definition_hash": preview.definition_hash,
        "args_hash": preview.args_hash,
        "run_id": str(preview.run_id) if preview.run_id else None,
        "attempt_count": preview.attempt_count,
        "failure": (
            {"code": preview.failure_code, "message": preview.failure_message}
            if preview.failure_code or preview.failure_message
            else None
        ),
        **dict(preview.preview_json or {}),
    }


def _now() -> datetime:
    return datetime.now(UTC)


def canonical_workflow_artifact_hash(payload: dict[str, Any]) -> str:
    return compute_definition_hash(payload)


def _identity_matches(
    preview: WorkflowPreviewArtifact,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    return all(
        (
            preview.tenant_id == tenant_id,
            preview.agent_id == agent_id,
            preview.session_id == session_id,
            preview.requested_by_user_id == user_id,
        )
    )


def claim_workflow_preview_record(
    preview: WorkflowPreviewArtifact,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    confirmation_source: str,
    confirmation_evidence_id: str,
    now: datetime | None = None,
    lease: timedelta = WORKFLOW_START_LEASE,
) -> str:
    current = now or _now()
    if not _identity_matches(
        preview,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        user_id=user_id,
    ):
        raise WorkflowConfirmationConflict(
            "identity_mismatch",
            "Workflow preview belongs to another tenant, agent, session, or user.",
        )
    if preview.expires_at <= current:
        preview.status = "expired"
        preview.claim_token = None
        preview.claim_expires_at = None
        raise WorkflowConfirmationConflict("preview_expired", "Workflow preview expired; create a new preview.")
    if preview.status == "started" and preview.run_id:
        return "replay"
    if preview.status == "starting" and preview.claim_expires_at and preview.claim_expires_at > current:
        raise WorkflowConfirmationConflict("start_in_progress", "Workflow start is already in progress.")
    if preview.status not in {"ready", "failed", "starting"}:
        raise WorkflowConfirmationConflict(
            "invalid_status",
            f"Workflow preview cannot start from status {preview.status}.",
        )
    source = str(confirmation_source or "").strip()
    evidence_id = str(confirmation_evidence_id or "").strip()
    if not source or not evidence_id:
        raise WorkflowConfirmationConflict(
            "confirmation_evidence_required",
            "Workflow start requires typed user confirmation evidence.",
        )

    preview.status = "starting"
    preview.run_id = preview.run_id or preview.id
    preview.claim_token = uuid.uuid4()
    preview.claim_expires_at = current + lease
    preview.attempt_count = int(preview.attempt_count or 0) + 1
    preview.confirmed_by_user_id = user_id
    preview.confirmation_source = source
    preview.confirmation_evidence_id = evidence_id
    preview.confirmed_at = current
    preview.failure_code = None
    preview.failure_message = None
    return "claimed"


def mark_workflow_preview_started_record(
    preview: WorkflowPreviewArtifact,
    *,
    run_id: uuid.UUID,
    claim_token: uuid.UUID | None = None,
    now: datetime | None = None,
) -> None:
    if claim_token is not None and preview.claim_token != claim_token:
        raise WorkflowConfirmationConflict("stale_claim", "Workflow preview start claim is stale.")
    if preview.run_id and preview.run_id != run_id:
        raise WorkflowConfirmationConflict("run_mismatch", "Workflow preview is bound to a different run.")
    preview.status = "started"
    preview.run_id = run_id
    preview.started_at = now or _now()
    preview.claim_token = None
    preview.claim_expires_at = None
    preview.failure_code = None
    preview.failure_message = None


def mark_workflow_preview_failed_record(
    preview: WorkflowPreviewArtifact,
    *,
    code: str,
    message: str,
    claim_token: uuid.UUID | None = None,
) -> None:
    if claim_token is not None and preview.claim_token != claim_token:
        raise WorkflowConfirmationConflict("stale_claim", "Workflow preview start claim is stale.")
    preview.status = "failed"
    preview.claim_token = None
    preview.claim_expires_at = None
    preview.failure_code = str(code)[:100]
    preview.failure_message = str(message)[:4000]


async def create_workflow_proposal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    proposal: dict[str, Any],
    now: datetime | None = None,
) -> WorkflowProposalArtifact:
    current = now or _now()
    proposal_id = uuid.uuid4()
    canonical = {**proposal, "proposal_id": str(proposal_id)}
    artifact = WorkflowProposalArtifact(
        id=proposal_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        requested_by_user_id=user_id,
        status="open",
        artifact_version=1,
        artifact_hash=canonical_workflow_artifact_hash(canonical),
        proposal_json=canonical,
        expires_at=current + WORKFLOW_CONFIRMATION_TTL,
    )
    db.add(artifact)
    await db.flush()
    return artifact


async def load_workflow_candidate(
    db: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    candidate_id: str,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    now: datetime | None = None,
) -> tuple[WorkflowProposalArtifact, dict[str, Any]]:
    result = await db.execute(
        select(WorkflowProposalArtifact).where(
            WorkflowProposalArtifact.id == proposal_id,
            WorkflowProposalArtifact.tenant_id == tenant_id,
            WorkflowProposalArtifact.agent_id == agent_id,
            WorkflowProposalArtifact.session_id == session_id,
            WorkflowProposalArtifact.requested_by_user_id == user_id,
        )
    )
    proposal = result.scalar_one_or_none()
    if proposal is None:
        raise WorkflowConfirmationConflict("proposal_not_found", "Dynamic Workflow proposal was not found.")
    if proposal.expires_at <= (now or _now()):
        proposal.status = "expired"
        raise WorkflowConfirmationConflict("proposal_expired", "Dynamic Workflow proposal expired.")
    for candidate in (proposal.proposal_json or {}).get("candidates") or []:
        if isinstance(candidate, dict) and candidate.get("candidate_id") == candidate_id:
            return proposal, candidate
    raise WorkflowConfirmationConflict("candidate_not_found", "Dynamic Workflow candidate was not found.")


async def create_workflow_preview(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    definition: dict[str, Any],
    args: dict[str, Any],
    definition_hash: str,
    args_hash: str,
    preview_payload: dict[str, Any],
    proposal: WorkflowProposalArtifact | None = None,
    candidate_id: str | None = None,
    now: datetime | None = None,
) -> WorkflowPreviewArtifact:
    current = now or _now()
    snapshot = {
        "definition": definition,
        "args": args,
        "definition_hash": definition_hash,
        "args_hash": args_hash,
        "proposal_id": str(proposal.id) if proposal else None,
        "candidate_id": candidate_id,
        "preview": preview_payload,
    }
    preview = WorkflowPreviewArtifact(
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        requested_by_user_id=user_id,
        proposal_id=proposal.id if proposal else None,
        candidate_id=candidate_id,
        status="ready",
        artifact_version=1,
        artifact_hash=canonical_workflow_artifact_hash(snapshot),
        definition_hash=definition_hash,
        args_hash=args_hash,
        definition_json=definition,
        args_json=args,
        preview_json=preview_payload,
        expires_at=current + WORKFLOW_CONFIRMATION_TTL,
    )
    db.add(preview)
    if proposal is not None:
        proposal.status = "previewed"
    await db.flush()
    return preview


async def load_workflow_preview(
    db: AsyncSession,
    *,
    preview_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID | None,
    user_id: uuid.UUID,
    for_update: bool = False,
) -> WorkflowPreviewArtifact:
    statement = select(WorkflowPreviewArtifact).where(
        WorkflowPreviewArtifact.id == preview_id,
        WorkflowPreviewArtifact.tenant_id == tenant_id,
        WorkflowPreviewArtifact.agent_id == agent_id,
        WorkflowPreviewArtifact.requested_by_user_id == user_id,
    )
    if session_id is not None:
        statement = statement.where(WorkflowPreviewArtifact.session_id == session_id)
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    preview = result.scalar_one_or_none()
    if preview is None:
        raise WorkflowConfirmationConflict("preview_not_found", "Workflow preview was not found for this session.")
    return preview


async def claim_workflow_preview_start(
    db: AsyncSession,
    *,
    preview_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID | None,
    user_id: uuid.UUID,
    confirmation_source: str,
    confirmation_evidence_id: str,
    now: datetime | None = None,
) -> WorkflowStartClaim:
    preview = await load_workflow_preview(
        db,
        preview_id=preview_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        user_id=user_id,
        for_update=True,
    )
    outcome = claim_workflow_preview_record(
        preview,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id or preview.session_id,
        user_id=user_id,
        confirmation_source=confirmation_source,
        confirmation_evidence_id=confirmation_evidence_id,
        now=now,
    )
    run_exists = False
    if preview.run_id:
        result = await db.execute(select(RuntimeTask.id).where(RuntimeTask.id == preview.run_id))
        run_exists = result.scalar_one_or_none() is not None
        if run_exists and outcome == "claimed":
            mark_workflow_preview_started_record(preview, run_id=preview.run_id, claim_token=preview.claim_token, now=now)
            outcome = "replay"
    await db.flush()
    return WorkflowStartClaim(outcome=outcome, preview=preview, run_exists=run_exists)
