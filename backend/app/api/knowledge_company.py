"""Company Knowledge authority and publication API."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import get_current_user
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import async_session, get_db
from app.models.company_knowledge import (
    CompanyKnowledgeImportJob,
    CompanyKnowledgeProposal,
    CompanyKnowledgeSourceContract,
)
from app.models.user import User
from app.services.company_knowledge_contracts import SourceContractInput
from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal
from app.services.company_knowledge_service import (
    CompanyEvidenceIngestRequest,
    CompanyKnowledgeProposalRequest,
    CompanyKnowledgeReviewRequest,
    CompanyKnowledgeService,
)


router = APIRouter(prefix="/knowledge/company", tags=["company-knowledge"])


def _service() -> CompanyKnowledgeService:
    return CompanyKnowledgeService(data_root=Path(get_settings().AGENT_DATA_DIR))


def _principal(current_user: User, tenant_id: uuid.UUID) -> CompanyKnowledgePrincipal:
    return CompanyKnowledgePrincipal(
        tenant_id=tenant_id,
        accountable_user_id=uuid.UUID(str(current_user.id)),
        accountable_role=str(current_user.role),
        actor_type="user",
        actor_id=uuid.UUID(str(current_user.id)),
        purpose="interactive_session",
        session_id=None,
    )


def _payload(value: Any) -> dict[str, Any]:
    encoded = jsonable_encoder(value, sqlalchemy_safe=True)
    if not isinstance(encoded, dict):
        raise TypeError("Company Knowledge response must be an object")
    return encoded


async def _call(awaitable: Any) -> Any:
    try:
        return await awaitable
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


async def _process_import(*, tenant_id: uuid.UUID, job_id: uuid.UUID) -> None:
    await _service().process_import_job(
        tenant_id=tenant_id,
        job_id=job_id,
        session_factory=async_session,
    )


def _schedule_import_processing(
    background_tasks: BackgroundTasks,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
) -> None:
    background_tasks.add_task(_process_import, tenant_id=tenant_id, job_id=job_id)


class SourceContractCreate(BaseModel):
    source_kind: str = Field(..., min_length=1, max_length=50)
    provider_kind: str = Field("native", min_length=1, max_length=80)
    stable_source_id: str = Field(..., min_length=1, max_length=300)
    owner_principal_ref: str = Field(..., min_length=1, max_length=300)
    accountable_steward_ref: str = Field(..., min_length=1, max_length=300)
    connection_ref: str | None = Field(None, max_length=512)
    schema_ref: str | None = Field(None, max_length=512)
    schema_version: str | None = Field(None, max_length=120)
    identity_keys: list[str] = Field(default_factory=list)
    relation_keys: list[str] = Field(default_factory=list)
    ingest_mode: str
    cursor_kind: str | None = Field(None, max_length=80)
    cursor_policy: dict[str, Any] = Field(default_factory=dict)
    watermark_field: str | None = Field(None, max_length=300)
    temporal_mapping: dict[str, Any] = Field(default_factory=dict)
    source_acl_mapping_policy: dict[str, Any]
    default_sensitivity: str
    export_policy: dict[str, Any] = Field(default_factory=dict)
    retention_policy: dict[str, Any] = Field(default_factory=dict)
    legal_hold_policy: dict[str, Any] = Field(default_factory=dict)
    allowed_namespaces: list[str] = Field(min_length=1)
    precedence_policy_ref: str | None = Field(None, max_length=512)
    acceptance_suite_ref: str | None = Field(None, max_length=512)
    idempotency_policy: dict[str, Any]
    idempotency_key: str = Field(..., min_length=1, max_length=300)
    trace_id: str = Field(..., min_length=1, max_length=300)

    def contract(self) -> SourceContractInput:
        return SourceContractInput(
            source_kind=self.source_kind,
            provider_kind=self.provider_kind,
            stable_source_id=self.stable_source_id,
            owner_principal_ref=self.owner_principal_ref,
            accountable_steward_ref=self.accountable_steward_ref,
            connection_ref=self.connection_ref,
            schema_ref=self.schema_ref,
            schema_version=self.schema_version,
            identity_keys=tuple(self.identity_keys),
            relation_keys=tuple(self.relation_keys),
            ingest_mode=self.ingest_mode,
            cursor_kind=self.cursor_kind,
            cursor_policy=self.cursor_policy,
            watermark_field=self.watermark_field,
            temporal_mapping=self.temporal_mapping,
            source_acl_mapping_policy=self.source_acl_mapping_policy,
            default_sensitivity=self.default_sensitivity,
            export_policy=self.export_policy,
            retention_policy=self.retention_policy,
            legal_hold_policy=self.legal_hold_policy,
            allowed_namespaces=tuple(self.allowed_namespaces),
            precedence_policy_ref=self.precedence_policy_ref,
            acceptance_suite_ref=self.acceptance_suite_ref,
            idempotency_policy=self.idempotency_policy,
        )


class EvidenceImportCreate(BaseModel):
    source_contract_id: uuid.UUID
    source_contract_version: int = Field(..., ge=1)
    evidence_kind: str
    source_item_id: str = Field(..., min_length=1, max_length=500)
    source_revision: str = Field("", max_length=300)
    title: str = Field(..., min_length=1, max_length=300)
    markdown: str | None = None
    typed_payload: dict[str, Any] | None = None
    external_artifact_ref: str | None = Field(None, max_length=1000)
    schema_ref: str | None = Field(None, max_length=1000)
    source_acl_snapshot: dict[str, Any]
    proposed_namespace: str = Field(..., min_length=1, max_length=300)
    proposed_sensitivity: str
    occurred_at: datetime | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    observed_at: datetime
    cursor: dict[str, Any] = Field(default_factory=dict)
    sequence: str | None = Field(None, max_length=300)
    coverage_ledger: dict[str, Any]
    purpose: str = Field(..., min_length=1, max_length=1000)
    idempotency_key: str = Field(..., min_length=1, max_length=300)
    trace_id: str = Field(..., min_length=1, max_length=300)

    def request(self) -> CompanyEvidenceIngestRequest:
        return CompanyEvidenceIngestRequest(**self.model_dump())


class ProposalCreate(BaseModel):
    proposal_kind: str
    source_id: uuid.UUID | None = None
    source_document_id: uuid.UUID | None = None
    source_revision_ref: str | None = Field(None, max_length=1000)
    baseline_publication_id: uuid.UUID | None = None
    baseline_version: int | None = Field(None, ge=1)
    proposed_patch: dict[str, Any]
    proposed_namespace: str = Field(..., min_length=1, max_length=300)
    proposed_sensitivity: str
    source_refs: list[str] = Field(min_length=1)
    source_coverage: dict[str, Any]
    conflict_candidates: list[dict[str, Any]] = Field(default_factory=list)
    ontology_mapping: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "normal"
    required_review_policy: dict[str, Any]
    idempotency_key: str = Field(..., min_length=1, max_length=300)
    trace_id: str = Field(..., min_length=1, max_length=300)

    def request(self) -> CompanyKnowledgeProposalRequest:
        values = self.model_dump()
        values["source_refs"] = tuple(values["source_refs"])
        values["conflict_candidates"] = tuple(values["conflict_candidates"])
        return CompanyKnowledgeProposalRequest(**values)


class ProposalSubmit(BaseModel):
    expected_state_version: int = Field(..., ge=1)
    trace_id: str = Field(..., min_length=1, max_length=300)


class ProposalReview(BaseModel):
    expected_state_version: int = Field(..., ge=1)
    decision: str
    reviewer_role: str = Field(..., min_length=1, max_length=80)
    reason: str = Field(..., min_length=1, max_length=10000)
    evidence_refs: list[str] = Field(min_length=1)
    policy_snapshot: dict[str, Any]
    trace_id: str = Field(..., min_length=1, max_length=300)

    def request(self) -> CompanyKnowledgeReviewRequest:
        return CompanyKnowledgeReviewRequest(
            decision=self.decision,
            reviewer_role=self.reviewer_role,
            reason=self.reason,
            evidence_refs=tuple(self.evidence_refs),
            policy_snapshot=self.policy_snapshot,
        )


class ProposalPublish(BaseModel):
    expected_state_version: int = Field(..., ge=1)
    valid_from: datetime
    valid_until: datetime | None = None
    trace_id: str = Field(..., min_length=1, max_length=300)


class PublicationRetire(BaseModel):
    reason: str = Field(..., min_length=1, max_length=10000)
    trace_id: str = Field(..., min_length=1, max_length=300)


class PublicationRestore(PublicationRetire):
    valid_from: datetime


@router.post("/source-contracts")
async def create_source_contract(
    body: SourceContractCreate,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    contract = await _call(
        _service().register_source_contract(
            db,
            principal=_principal(current_user, target_tenant),
            contract_input=body.contract(),
            idempotency_key=body.idempotency_key,
            trace_id=body.trace_id,
        )
    )
    await db.commit()
    return _payload(contract)


@router.get("/source-contracts")
async def list_source_contracts(
    tenant_id: uuid.UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    rows = (
        (
            await db.execute(
                select(CompanyKnowledgeSourceContract)
                .where(CompanyKnowledgeSourceContract.tenant_id == target_tenant)
                .order_by(
                    CompanyKnowledgeSourceContract.stable_source_id,
                    CompanyKnowledgeSourceContract.version.desc(),
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {"source_contracts": [_payload(row) for row in rows]}


@router.get("/source-contracts/{contract_id}")
async def get_source_contract(
    contract_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    row = (
        await db.execute(
            select(CompanyKnowledgeSourceContract).where(
                CompanyKnowledgeSourceContract.id == contract_id,
                CompanyKnowledgeSourceContract.tenant_id == target_tenant,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="company_source_contract_not_found")
    return _payload(row)


@router.post("/imports", status_code=status.HTTP_202_ACCEPTED)
async def create_import(
    background_tasks: BackgroundTasks,
    body: EvidenceImportCreate,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    job = await _call(
        _service().queue_evidence_import(
            db,
            principal=_principal(current_user, target_tenant),
            request=body.request(),
        )
    )
    await db.commit()
    _schedule_import_processing(background_tasks, tenant_id=target_tenant, job_id=job.id)
    return _payload(job)


@router.get("/import-jobs/{job_id}")
async def get_import_job(
    job_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    row = (
        await db.execute(
            select(CompanyKnowledgeImportJob).where(
                CompanyKnowledgeImportJob.id == job_id,
                CompanyKnowledgeImportJob.tenant_id == target_tenant,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="company_knowledge_import_job_not_found")
    return _payload(row)


@router.post("/proposals")
async def create_proposal(
    body: ProposalCreate,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    proposal = await _call(
        _service().create_proposal(
            db,
            principal=_principal(current_user, target_tenant),
            request=body.request(),
        )
    )
    await db.commit()
    return _payload(proposal)


@router.get("/proposals")
async def list_proposals(
    tenant_id: uuid.UUID | None = Query(None),
    proposal_status: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    statement = select(CompanyKnowledgeProposal).where(CompanyKnowledgeProposal.tenant_id == target_tenant)
    if proposal_status:
        statement = statement.where(CompanyKnowledgeProposal.status == proposal_status)
    rows = (
        (await db.execute(statement.order_by(CompanyKnowledgeProposal.created_at.desc()).limit(limit))).scalars().all()
    )
    return {"proposals": [_payload(row) for row in rows]}


@router.get("/proposals/{proposal_id}")
async def get_proposal(
    proposal_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    row = (
        await db.execute(
            select(CompanyKnowledgeProposal).where(
                CompanyKnowledgeProposal.id == proposal_id,
                CompanyKnowledgeProposal.tenant_id == target_tenant,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="company_knowledge_proposal_not_found")
    return _payload(row)


@router.post("/proposals/{proposal_id}/submit")
async def submit_proposal(
    proposal_id: uuid.UUID,
    body: ProposalSubmit,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    proposal = await _call(
        _service().submit_proposal(
            db,
            principal=_principal(current_user, target_tenant),
            proposal_id=proposal_id,
            expected_state_version=body.expected_state_version,
            trace_id=body.trace_id,
        )
    )
    await db.commit()
    return _payload(proposal)


@router.post("/proposals/{proposal_id}/review")
async def review_proposal(
    proposal_id: uuid.UUID,
    body: ProposalReview,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    proposal = await _call(
        _service().record_review(
            db,
            principal=_principal(current_user, target_tenant),
            proposal_id=proposal_id,
            request=body.request(),
            expected_state_version=body.expected_state_version,
            trace_id=body.trace_id,
        )
    )
    await db.commit()
    return _payload(proposal)


@router.post("/proposals/{proposal_id}/publish")
async def publish_proposal(
    proposal_id: uuid.UUID,
    body: ProposalPublish,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    publication = await _call(
        _service().publish_proposal(
            db,
            principal=_principal(current_user, target_tenant),
            proposal_id=proposal_id,
            expected_state_version=body.expected_state_version,
            valid_from=body.valid_from,
            valid_until=body.valid_until,
            trace_id=body.trace_id,
        )
    )
    await db.commit()
    return _payload(publication)


@router.post("/publications/{publication_id}/retire")
async def retire_publication(
    publication_id: uuid.UUID,
    body: PublicationRetire,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    publication = await _call(
        _service().retire_publication(
            db,
            principal=_principal(current_user, target_tenant),
            publication_id=publication_id,
            reason=body.reason,
            trace_id=body.trace_id,
        )
    )
    await db.commit()
    return _payload(publication)


@router.post("/publications/{publication_id}/restore")
async def restore_publication(
    publication_id: uuid.UUID,
    body: PublicationRestore,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    publication = await _call(
        _service().restore_publication(
            db,
            principal=_principal(current_user, target_tenant),
            publication_id=publication_id,
            reason=body.reason,
            valid_from=body.valid_from,
            trace_id=body.trace_id,
        )
    )
    await db.commit()
    return _payload(publication)


__all__ = ["router"]
