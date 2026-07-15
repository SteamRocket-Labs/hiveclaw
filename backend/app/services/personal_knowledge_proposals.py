"""Governed Agent -> Personal Knowledge Base proposal policy and commit service."""

from __future__ import annotations

import hashlib
import difflib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select

from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.config_revision import ConfigRevision
from app.models.knowledge import KnowledgeDocument, KnowledgeGrant, PersonalKnowledgeProposal
from app.services.config_versioning import get_history, save_revision

from app.services.privacy_layer import (
    PrivacyLayer,
    SensitivityLevel,
    canonicalize_sensitivity,
    max_sensitivity,
    sensitivity_rank,
)


@dataclass(frozen=True, slots=True)
class ProposalContentDecision:
    outcome: str
    content: str
    sensitivity: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PersonalKnowledgeProposalView:
    proposal_id: uuid.UUID
    owner_user_id: uuid.UUID
    proposed_by_agent_id: uuid.UUID
    delegated_by_agent_id: uuid.UUID | None
    delegation_id: str | None
    title: str
    content: str
    content_hash: str
    baseline_document_id: uuid.UUID | None
    baseline_revision_id: uuid.UUID | None
    baseline_content_hash: str | None
    diff_unified: str
    target_collection: str
    source_refs: list[str]
    sensitivity: str
    purpose: str
    dedupe_key: str
    idempotency_key: str
    policy_outcome: str
    policy_reason_codes: list[str]
    status: str
    review_reason: str | None
    document_id: uuid.UUID | None
    revision_id: uuid.UUID | None
    rollback_ref: str | None
    created_at: Any
    updated_at: Any


@dataclass(frozen=True, slots=True)
class PersonalKnowledgeRollbackResult:
    document_id: uuid.UUID
    version: int
    rollback_of_version: int
    revision_id: uuid.UUID
    rollback_ref: str


class PersonalKnowledgeProposalRejected(ValueError):
    """Fail-closed authority rejection before candidate content persistence."""


def evaluate_proposal_content(
    *,
    content: str,
    declared_sensitivity: str,
    max_chars: int,
) -> ProposalContentDecision:
    """Apply deterministic platform safety checks before an Agent proposal is stored.

    Semantic usefulness remains the Agent/owner's judgment. This function only
    enforces mechanical size and zero-retention privacy invariants.
    """

    clean_content = str(content or "").strip()
    if not clean_content:
        return ProposalContentDecision("reject", "", SensitivityLevel.PL1_PUBLIC.value, ("content_empty",))
    if len(clean_content) > max(1, int(max_chars)):
        return ProposalContentDecision(
            "reject",
            "",
            SensitivityLevel.PL1_PUBLIC.value,
            ("content_too_large",),
        )

    privacy = PrivacyLayer().classify_and_mask(clean_content)
    inferred_rank = sensitivity_rank(privacy.sensitivity)
    try:
        canonical_declared = canonicalize_sensitivity(declared_sensitivity or "internal")
    except ValueError:
        return ProposalContentDecision(
            "reject",
            privacy.sanitized_text,
            privacy.sensitivity.value,
            ("invalid_sensitivity",),
        )
    declared_rank = sensitivity_rank(canonical_declared)
    effective_sensitivity = max_sensitivity(privacy.sensitivity, canonical_declared)
    effective_rank = sensitivity_rank(effective_sensitivity)
    if privacy.rejected or effective_rank == 4:
        return ProposalContentDecision(
            "reject",
            privacy.sanitized_text,
            SensitivityLevel.PL4_CREDENTIAL.value,
            ("credential_zero_retention",),
        )

    reasons: list[str] = []
    if inferred_rank > declared_rank:
        reasons.append("sensitivity_upgraded")
    return ProposalContentDecision(
        "ask",
        privacy.sanitized_text,
        effective_sensitivity.value,
        tuple(reasons),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_personal_knowledge_unified_diff(*, previous: str, proposed: str) -> str:
    """Build the immutable owner-review diff stored with a proposal."""

    return "".join(
        difflib.unified_diff(
            str(previous or "").splitlines(keepends=True),
            str(proposed or "").splitlines(keepends=True),
            fromfile="current",
            tofile="proposed",
            lineterm="\n",
        )
    )


def _proposal_source_sha256(*, owner_user_id: uuid.UUID, target_collection: str, dedupe_key: str) -> str:
    return _sha256(f"agent-proposal\n{owner_user_id}\n{target_collection}\n{dedupe_key}")


def _proposal_view(row: PersonalKnowledgeProposal) -> PersonalKnowledgeProposalView:
    return PersonalKnowledgeProposalView(
        proposal_id=row.id,
        owner_user_id=row.owner_user_id,
        proposed_by_agent_id=row.proposed_by_agent_id,
        delegated_by_agent_id=row.delegated_by_agent_id,
        delegation_id=row.delegation_id,
        title=row.title,
        content=row.proposed_content,
        content_hash=row.content_hash,
        baseline_document_id=row.baseline_document_id,
        baseline_revision_id=row.baseline_revision_id,
        baseline_content_hash=row.baseline_content_hash,
        diff_unified=row.diff_unified,
        target_collection=row.target_collection,
        source_refs=[str(ref) for ref in list(row.source_refs_json or [])],
        sensitivity=row.sensitivity,
        purpose=row.purpose,
        dedupe_key=row.dedupe_key,
        idempotency_key=row.idempotency_key,
        policy_outcome=row.policy_outcome,
        policy_reason_codes=[str(code) for code in list(row.policy_reason_codes_json or [])],
        status=row.status,
        review_reason=row.review_reason,
        document_id=row.document_id,
        revision_id=row.revision_id,
        rollback_ref=row.rollback_ref,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _owner_id(agent: Any) -> uuid.UUID | None:
    raw = (
        getattr(agent, "owner_user_id", None)
        or getattr(agent, "sponsor_user_id", None)
        or getattr(agent, "creator_id", None)
    )
    return uuid.UUID(str(raw)) if raw else None


def _normalize_collection(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "inbox").strip().lower()).strip("-")
    normalized = normalized or "inbox"
    if len(normalized) > 120:
        raise ValueError("target_collection exceeds 120 characters")
    return normalized


def _normalize_refs(values: list[str] | tuple[str, ...] | None) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in (values or []) if str(value).strip()))


class PersonalKnowledgeProposalService:
    """Single transaction boundary for proposal, owner decision, and revision."""

    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        knowledge_service: Any | None = None,
        max_content_chars: int = 100_000,
    ) -> None:
        from app.config import get_settings

        self.data_root = Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)
        self.knowledge_service = knowledge_service
        self.max_content_chars = max(1, int(max_content_chars))

    def _knowledge_service(self):
        if self.knowledge_service is not None:
            return self.knowledge_service
        from app.services.personal_knowledge_service import PersonalKnowledgeService

        self.knowledge_service = PersonalKnowledgeService(data_root=self.data_root)
        return self.knowledge_service

    async def _audit_authority_rejection(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        proposed_by_agent_id: uuid.UUID,
        reason: str,
    ) -> None:
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                user_id=owner_user_id,
                agent_id=proposed_by_agent_id,
                action="personal_kb.proposal.rejected_authority",
                details={"reason": reason, "owner_user_id": str(owner_user_id)},
            )
        )
        await session.flush()

    async def _validate_authority(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        proposed_by_agent_id: uuid.UUID,
        delegation_token: Any | None,
    ) -> tuple[uuid.UUID | None, str | None]:
        result = await session.execute(
            select(Agent).where(
                Agent.id == proposed_by_agent_id,
                Agent.tenant_id == tenant_id,
                Agent.deleted_at.is_(None),
            )
        )
        agent = result.scalar_one_or_none()
        if agent is None or _owner_id(agent) != owner_user_id:
            await self._audit_authority_rejection(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                proposed_by_agent_id=proposed_by_agent_id,
                reason="agent_owner_mismatch",
            )
            raise PersonalKnowledgeProposalRejected("agent_owner_mismatch")

        if delegation_token is None:
            return None, None
        from app.agents.delegation_token import validate_delegation_token

        validation = validate_delegation_token(
            delegation_token,
            child_agent_id=proposed_by_agent_id,
            capability="agent.knowledge.propose",
        )
        if not validation.valid:
            await self._audit_authority_rejection(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                proposed_by_agent_id=proposed_by_agent_id,
                reason=f"delegation_invalid:{validation.reason}",
            )
            raise PersonalKnowledgeProposalRejected(f"delegation_invalid:{validation.reason}")
        return delegation_token.parent_agent_id, delegation_token.delegation_id

    async def _auto_approve_grant(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        agent_id: uuid.UUID,
    ) -> bool:
        result = await session.execute(
            select(KnowledgeGrant).where(
                KnowledgeGrant.tenant_id == tenant_id,
                KnowledgeGrant.scope_type == "person",
                KnowledgeGrant.scope_id == owner_user_id,
                KnowledgeGrant.resource_type == "scope",
                KnowledgeGrant.resource_id == owner_user_id,
                KnowledgeGrant.grantee_type == "agent",
                KnowledgeGrant.grantee_id == agent_id,
                KnowledgeGrant.permission == "manage",
                KnowledgeGrant.requester_user_id == owner_user_id,
                KnowledgeGrant.purpose == "autonomous_agent",
                KnowledgeGrant.session_id.is_(None),
                KnowledgeGrant.delegation_id.is_(None),
                KnowledgeGrant.revoked_at.is_(None),
                or_(KnowledgeGrant.expires_at.is_(None), KnowledgeGrant.expires_at > datetime.now(UTC)),
            )
        )
        grant = result.scalar_one_or_none()
        return bool(grant and dict(grant.grant_metadata_json or {}).get("proposal_mode") == "auto_approve")

    async def propose(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        proposed_by_agent_id: uuid.UUID,
        title: str,
        content: str,
        target_collection: str,
        sensitivity: str,
        source_refs: list[str] | tuple[str, ...],
        purpose: str,
        dedupe_key: str,
        idempotency_key: str,
        delegation_token: Any | None = None,
        session_id: str | None = None,
        runtime_task_id: str | None = None,
    ) -> PersonalKnowledgeProposalView:
        clean_idempotency_key = str(idempotency_key or "").strip()
        if not clean_idempotency_key:
            raise ValueError("idempotency_key is required")
        if len(clean_idempotency_key) > 200:
            raise ValueError("idempotency_key exceeds 200 characters")
        existing = (
            await session.execute(
                select(PersonalKnowledgeProposal).where(
                    PersonalKnowledgeProposal.tenant_id == tenant_id,
                    PersonalKnowledgeProposal.idempotency_key == clean_idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _proposal_view(existing)

        delegated_by, delegation_id = await self._validate_authority(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            proposed_by_agent_id=proposed_by_agent_id,
            delegation_token=delegation_token,
        )
        refs = _normalize_refs(source_refs)
        if session_id:
            refs = _normalize_refs([*refs, f"session://{session_id}"])
        if runtime_task_id:
            refs = _normalize_refs([*refs, f"runtime-task://{runtime_task_id}"])
        if not refs:
            raise ValueError("at least one source_ref is required")

        decision = evaluate_proposal_content(
            content=content,
            declared_sensitivity=sensitivity,
            max_chars=self.max_content_chars,
        )
        clean_title = str(title or "").strip()
        clean_purpose = str(purpose or "").strip()
        if not clean_title:
            raise ValueError("title is required")
        if len(clean_title) > 300:
            raise ValueError("title exceeds 300 characters")
        if not clean_purpose:
            raise ValueError("purpose is required")
        clean_dedupe_key = str(dedupe_key or "").strip() or _sha256(decision.content)
        if len(clean_dedupe_key) > 128:
            raise ValueError("dedupe_key exceeds 128 characters")
        content_hash = _sha256(decision.content)
        normalized_collection = _normalize_collection(target_collection)
        source_sha256 = _proposal_source_sha256(
            owner_user_id=owner_user_id,
            target_collection=normalized_collection,
            dedupe_key=clean_dedupe_key,
        )
        baseline_document = (
            await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.scope_type == "person",
                    KnowledgeDocument.scope_id == owner_user_id,
                    KnowledgeDocument.source_sha256 == source_sha256,
                )
            )
        ).scalar_one_or_none()
        baseline_revision = None
        baseline_content = ""
        if baseline_document is not None:
            baseline_revision = (
                await session.execute(
                    select(ConfigRevision)
                    .where(
                        ConfigRevision.entity_type == "personal_knowledge_document",
                        ConfigRevision.entity_id == baseline_document.id,
                        ConfigRevision.tenant_id == tenant_id,
                    )
                    .order_by(ConfigRevision.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if baseline_revision is not None:
                baseline_content = str(dict(baseline_revision.content or {}).get("markdown") or "")

        duplicate_document = (
            await session.execute(
                select(KnowledgeDocument.id).where(
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.scope_type == "person",
                    KnowledgeDocument.scope_id == owner_user_id,
                    KnowledgeDocument.canonical_md_sha256 == content_hash,
                )
            )
        ).scalar_one_or_none()
        duplicate_proposal = (
            await session.execute(
                select(PersonalKnowledgeProposal.id).where(
                    PersonalKnowledgeProposal.tenant_id == tenant_id,
                    PersonalKnowledgeProposal.owner_user_id == owner_user_id,
                    PersonalKnowledgeProposal.content_hash == content_hash,
                    PersonalKnowledgeProposal.status.in_(("pending", "approved", "committed")),
                )
            )
        ).scalar_one_or_none()
        reasons = list(decision.reason_codes)
        policy_outcome = decision.outcome
        if duplicate_document is not None or duplicate_proposal is not None:
            policy_outcome = "reject"
            reasons.append("duplicate_content")
        elif policy_outcome != "reject" and await self._auto_approve_grant(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            agent_id=proposed_by_agent_id,
        ):
            policy_outcome = "approve"
            reasons.append("manage_grant_auto_approve")

        status = "rejected" if policy_outcome == "reject" else "approved" if policy_outcome == "approve" else "pending"
        row = PersonalKnowledgeProposal(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            proposed_by_agent_id=proposed_by_agent_id,
            delegated_by_agent_id=delegated_by,
            delegation_id=delegation_id,
            title=clean_title,
            proposed_content=decision.content,
            content_hash=content_hash,
            baseline_document_id=baseline_document.id if baseline_document is not None else None,
            baseline_revision_id=baseline_revision.id if baseline_revision is not None else None,
            baseline_content_hash=_sha256(baseline_content) if baseline_revision is not None else None,
            diff_unified=build_personal_knowledge_unified_diff(
                previous=baseline_content,
                proposed=decision.content,
            ),
            target_collection=normalized_collection,
            source_refs_json=refs,
            sensitivity=decision.sensitivity,
            purpose=clean_purpose,
            dedupe_key=clean_dedupe_key,
            idempotency_key=clean_idempotency_key,
            policy_outcome=policy_outcome,
            policy_reason_codes_json=list(dict.fromkeys(reasons)),
            status=status,
        )
        session.add(row)
        await session.flush()
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                user_id=owner_user_id,
                agent_id=proposed_by_agent_id,
                action="personal_kb.proposal.created",
                details={
                    "proposal_id": str(row.id),
                    "policy_outcome": policy_outcome,
                    "reason_codes": list(row.policy_reason_codes_json or []),
                    "source_refs": refs,
                    "content_hash": content_hash,
                },
            )
        )
        if policy_outcome == "approve":
            await self._commit(session, row=row, reviewer_user_id=owner_user_id, review_reason="policy_auto_approved")
        await session.flush()
        return _proposal_view(row)

    async def _commit(
        self,
        session: Any,
        *,
        row: PersonalKnowledgeProposal,
        reviewer_user_id: uuid.UUID,
        review_reason: str | None,
    ) -> None:
        source_sha256 = _proposal_source_sha256(
            owner_user_id=row.owner_user_id,
            target_collection=row.target_collection,
            dedupe_key=row.dedupe_key,
        )
        result = await self._knowledge_service().queue_markdown_import(
            session,
            tenant_id=row.tenant_id,
            owner_user_id=row.owner_user_id,
            title=row.title,
            markdown=row.proposed_content,
            source_kind="agent_proposal",
            source_uri=f"proposal://{row.id}",
            source_sha256=source_sha256,
            created_by_user_id=reviewer_user_id,
            agent_searchable=True,
            sensitivity=row.sensitivity,
            doc_metadata={
                "proposal_id": str(row.id),
                "target_collection": row.target_collection,
                "purpose": row.purpose,
                "source_refs": list(row.source_refs_json or []),
                "proposed_by_agent_id": str(row.proposed_by_agent_id),
            },
        )
        revision = await save_revision(
            session,
            entity_type="personal_knowledge_document",
            entity_id=result.document_id,
            tenant_id=row.tenant_id,
            content={
                "schema": "hive.personal_knowledge_document.v1",
                "title": row.title,
                "markdown": row.proposed_content,
                "canonical_md_sha256": result.artifact_hash,
                "source_sha256": source_sha256,
                "source_refs": list(row.source_refs_json or []),
                "target_collection": row.target_collection,
                "sensitivity": row.sensitivity,
                "purpose": row.purpose,
                "agent_searchable": True,
                "dedupe_key": row.dedupe_key,
            },
            change_source="agent_proposal",
            changed_by_user_id=reviewer_user_id,
            changed_by_agent_id=row.proposed_by_agent_id,
            change_message=review_reason or "Personal KB proposal approved",
        )
        row.status = "committed"
        row.reviewed_by_user_id = reviewer_user_id
        row.reviewed_at = datetime.now(UTC)
        row.updated_at = row.reviewed_at
        row.review_reason = review_reason
        row.document_id = result.document_id
        row.revision_id = revision.id
        row.rollback_ref = f"personal-kb://documents/{result.document_id}/revisions/{revision.version}"
        session.add(
            AuditLog(
                tenant_id=row.tenant_id,
                user_id=reviewer_user_id,
                agent_id=row.proposed_by_agent_id,
                action="personal_kb.proposal.commit",
                details={
                    "proposal_id": str(row.id),
                    "document_id": str(result.document_id),
                    "revision_id": str(revision.id),
                    "version": revision.version,
                    "rollback_ref": row.rollback_ref,
                    "source_refs": list(row.source_refs_json or []),
                },
            )
        )

    async def review(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        proposal_id: uuid.UUID,
        reviewer_user_id: uuid.UUID,
        decision: str,
        reason: str | None = None,
    ) -> PersonalKnowledgeProposalView:
        row = (
            await session.execute(
                select(PersonalKnowledgeProposal)
                .where(
                    PersonalKnowledgeProposal.id == proposal_id,
                    PersonalKnowledgeProposal.tenant_id == tenant_id,
                    PersonalKnowledgeProposal.owner_user_id == owner_user_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError("personal knowledge proposal not found")
        if reviewer_user_id != owner_user_id:
            raise PermissionError("only the owner can review a personal knowledge proposal")
        if row.status == "committed":
            return _proposal_view(row)
        clean_decision = str(decision or "").strip().lower()
        if clean_decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        if row.status == "rejected":
            return _proposal_view(row)
        if clean_decision == "reject":
            row.status = "rejected"
            row.policy_outcome = "reject"
            row.reviewed_by_user_id = reviewer_user_id
            row.reviewed_at = datetime.now(UTC)
            row.updated_at = row.reviewed_at
            row.review_reason = str(reason or "").strip() or None
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    user_id=reviewer_user_id,
                    agent_id=row.proposed_by_agent_id,
                    action="personal_kb.proposal.reject",
                    details={"proposal_id": str(row.id), "reason": row.review_reason},
                )
            )
        else:
            row.status = "approved"
            row.policy_outcome = "approve"
            await self._commit(
                session,
                row=row,
                reviewer_user_id=reviewer_user_id,
                review_reason=str(reason or "").strip() or None,
            )
        await session.flush()
        return _proposal_view(row)

    async def list_proposals(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        statuses: tuple[str, ...] | None = None,
        limit: int = 100,
    ) -> list[PersonalKnowledgeProposalView]:
        query = select(PersonalKnowledgeProposal).where(
            PersonalKnowledgeProposal.tenant_id == tenant_id,
            PersonalKnowledgeProposal.owner_user_id == owner_user_id,
        )
        if statuses:
            query = query.where(PersonalKnowledgeProposal.status.in_(tuple(statuses)))
        result = await session.execute(
            query.order_by(PersonalKnowledgeProposal.created_at.desc(), PersonalKnowledgeProposal.id.desc()).limit(
                max(1, min(200, int(limit)))
            )
        )
        return [_proposal_view(row) for row in result.scalars().all()]

    async def revision_history(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        document = (
            await session.execute(
                select(KnowledgeDocument.id).where(
                    KnowledgeDocument.id == document_id,
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.scope_type == "person",
                    KnowledgeDocument.scope_id == owner_user_id,
                )
            )
        ).scalar_one_or_none()
        if document is None:
            raise ValueError("personal knowledge document not found")
        return await get_history(
            session,
            "personal_knowledge_document",
            document_id,
            tenant_id=tenant_id,
        )

    async def rollback_document(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        target_version: int,
        reviewer_user_id: uuid.UUID,
    ) -> PersonalKnowledgeRollbackResult:
        if reviewer_user_id != owner_user_id:
            raise PermissionError("only the owner can roll back a personal knowledge document")
        document = (
            await session.execute(
                select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.id == document_id,
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.scope_type == "person",
                    KnowledgeDocument.scope_id == owner_user_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if document is None:
            raise ValueError("personal knowledge document not found")
        target = (
            await session.execute(
                select(ConfigRevision).where(
                    ConfigRevision.entity_type == "personal_knowledge_document",
                    ConfigRevision.entity_id == document_id,
                    ConfigRevision.tenant_id == tenant_id,
                    ConfigRevision.version == int(target_version),
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise ValueError("personal knowledge revision not found")
        content = dict(target.content or {})
        result = await self._knowledge_service().ingest_markdown(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            title=str(content.get("title") or document.title),
            markdown=str(content.get("markdown") or ""),
            source_kind="agent_proposal",
            source_uri=document.source_uri,
            source_sha256=str(document.source_sha256),
            created_by_user_id=reviewer_user_id,
            agent_searchable=bool(content.get("agent_searchable", True)),
            sensitivity=str(content.get("sensitivity") or document.sensitivity),
            doc_metadata={
                **dict(document.doc_metadata_json or {}),
                "rollback_of_version": int(target_version),
                "rollback_of_revision_id": str(target.id),
            },
            force_reindex=True,
        )
        revision = await save_revision(
            session,
            entity_type="personal_knowledge_document",
            entity_id=document_id,
            tenant_id=tenant_id,
            content=content,
            change_source="rollback",
            changed_by_user_id=reviewer_user_id,
            change_message=f"Rolled back Personal KB document to version {target_version}",
            rollback_of_revision_id=target.id,
            force_revision=True,
        )
        rollback_ref = f"personal-kb://documents/{document_id}/revisions/{revision.version}"
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                user_id=reviewer_user_id,
                action="personal_kb.document.rollback",
                details={
                    "document_id": str(document_id),
                    "target_version": int(target_version),
                    "target_revision_id": str(target.id),
                    "result_revision_id": str(revision.id),
                    "result_artifact_hash": result.artifact_hash,
                    "rollback_ref": rollback_ref,
                },
            )
        )
        await session.flush()
        return PersonalKnowledgeRollbackResult(
            document_id=document_id,
            version=revision.version,
            rollback_of_version=int(target_version),
            revision_id=revision.id,
            rollback_ref=rollback_ref,
        )
