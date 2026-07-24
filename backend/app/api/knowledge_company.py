"""Company Knowledge authority and publication API."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal
import uuid

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import get_current_user
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import async_session, get_db
from app.models.company_knowledge import (
    CompanyKnowledgeImportJob,
    CompanyKnowledgeSourceContract,
)
from app.models.company_ontology import (
    CompanyOntologyActivation,
    CompanyOntologyCurationRun,
    CompanyOntologyPackage,
    CompanyOntologyPackageInstallation,
    CompanyOntologyPackageVersion,
    CompanyOntologyRelease,
)
from app.models.user import User
from app.services.company_knowledge_contracts import (
    SourceContractInput,
    default_company_knowledge_review_policy,
)
from app.services.company_knowledge_control_plane import (
    CompanyKnowledgePermissionGrantInput,
    CompanyKnowledgePermissionService,
)
from app.services.company_knowledge_gateway import (
    CompanyKnowledgeDocumentListRequest,
    CompanyKnowledgeGateway,
    CompanyKnowledgeReadRequest,
    CompanyKnowledgeSearchRequest,
    CompanyKnowledgeSourceExplainRequest,
)
from app.services.company_knowledge_permissions import (
    CompanyKnowledgePrincipal,
    CompanyKnowledgeResource,
    resolve_company_knowledge_permission,
)
from app.services.company_knowledge_promotion import (
    CompanyKnowledgePromotionService,
    LegacyPromotionIntakeRequest,
    PersonalPromotionIntakeRequest,
)
from app.services.company_knowledge_service import (
    CompanyEvidenceIngestRequest,
    CompanyKnowledgeMaterializationRequest,
    CompanyKnowledgeProposalRequest,
    CompanyKnowledgeReviewRequest,
    CompanyKnowledgeService,
)
from app.services.company_ontology_contracts import load_builtin_ontology_catalog
from app.services.company_ontology_engine import OntologyEngineUnavailable, ReferenceOntologyEngine
from app.services.company_ontology_gateway import (
    CompanyOntologyGateway,
    OntologyActionSimulationRequest,
    OntologyFactExplainRequest,
    OntologyObjectReadRequest,
    OntologyQueryRequest,
)
from app.services.company_ontology_service import (
    CompanyOntologyService,
    OntologyActivationRequest,
    OntologyPackageInstallRequest,
    OntologyReleaseLifecycleRequest,
)


router = APIRouter(prefix="/knowledge/company", tags=["company-knowledge"])


def _service() -> CompanyKnowledgeService:
    return CompanyKnowledgeService(data_root=Path(get_settings().AGENT_DATA_DIR))


def _gateway() -> CompanyKnowledgeGateway:
    return CompanyKnowledgeGateway()


def _permission_service() -> CompanyKnowledgePermissionService:
    return CompanyKnowledgePermissionService(proposal_authority=_service())


def _promotion_service() -> CompanyKnowledgePromotionService:
    return CompanyKnowledgePromotionService(
        data_root=Path(get_settings().AGENT_DATA_DIR),
        company_service=_service(),
    )


def _ontology_service() -> CompanyOntologyService:
    return CompanyOntologyService(knowledge_service=_service())


def _ontology_gateway() -> CompanyOntologyGateway:
    return CompanyOntologyGateway()


def _principal(current_user: User, tenant_id: uuid.UUID) -> CompanyKnowledgePrincipal:
    return CompanyKnowledgePrincipal(
        tenant_id=tenant_id,
        accountable_user_id=uuid.UUID(str(current_user.id)),
        accountable_role=str(current_user.role),
        actor_type="user",
        actor_id=uuid.UUID(str(current_user.id)),
        department_id=(
            uuid.UUID(str(current_user.department_id)) if getattr(current_user, "department_id", None) else None
        ),
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
    except OntologyEngineUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="company_ontology_engine_unavailable",
        ) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


async def _require_ontology_discover(
    db: AsyncSession,
    *,
    principal: CompanyKnowledgePrincipal,
    namespace: str,
) -> dict[str, Any]:
    decision = await resolve_company_knowledge_permission(
        db,
        principal=principal,
        resource=CompanyKnowledgeResource(
            tenant_id=principal.tenant_id,
            resource_type="company_ontology_namespace",
            resource_id=None,
            resource_key=f"namespace:{namespace}",
            namespace=namespace,
            sensitivity="PL1_public",
            source_acl_snapshot_hash=None,
            source_acl=None,
            evidence_access_complete=True,
            publication_status=None,
        ),
        action="discover",
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail="company_ontology_metadata_denied")
    return decision.evidence()


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
    model_config = ConfigDict(extra="forbid")

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
    idempotency_key: str = Field(..., min_length=1, max_length=300)
    trace_id: str = Field(..., min_length=1, max_length=300)

    def request(self) -> CompanyKnowledgeProposalRequest:
        values = self.model_dump()
        values["source_refs"] = tuple(values["source_refs"])
        values["conflict_candidates"] = tuple(values["conflict_candidates"])
        values["required_review_policy"] = default_company_knowledge_review_policy(
            proposed_sensitivity=self.proposed_sensitivity,
            risk_level=self.risk_level,
            created_by_type="user",
        )
        return CompanyKnowledgeProposalRequest(**values)


class ProposalSubmit(BaseModel):
    expected_state_version: int = Field(..., ge=1)
    trace_id: str = Field(..., min_length=1, max_length=300)


class ProposalReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_state_version: int = Field(..., ge=1)
    decision: str
    reason: str = Field(..., min_length=1, max_length=10000)
    evidence_refs: list[str] = Field(min_length=1)
    trace_id: str = Field(..., min_length=1, max_length=300)

    def request(self, *, reviewer_role: str) -> CompanyKnowledgeReviewRequest:
        return CompanyKnowledgeReviewRequest(
            decision=self.decision,
            reviewer_role=reviewer_role,
            reason=self.reason,
            evidence_refs=tuple(self.evidence_refs),
            policy_snapshot={},
        )


class ProposalMaterialize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_state_version: int = Field(..., ge=1)
    expected_proposed_content_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    title: str = Field(..., min_length=1, max_length=300)
    markdown: str = Field(..., min_length=1, max_length=50 * 1024 * 1024)
    attest_candidate_applied: Literal[True]
    idempotency_key: str = Field(..., min_length=1, max_length=300)
    trace_id: str = Field(..., min_length=1, max_length=300)

    def request(self) -> CompanyKnowledgeMaterializationRequest:
        return CompanyKnowledgeMaterializationRequest(
            title=self.title,
            markdown=self.markdown,
            expected_proposed_content_hash=self.expected_proposed_content_hash,
            attest_candidate_applied=self.attest_candidate_applied,
            idempotency_key=self.idempotency_key,
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


class CompanyPermissionGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_type: str
    principal_id: uuid.UUID | None = None
    principal_key: str | None = Field(None, max_length=300)
    resource_type: str
    resource_id: uuid.UUID | None = None
    resource_key: str | None = Field(None, max_length=500)
    actions: list[str] = Field(min_length=1, max_length=30)
    effect: str
    sensitivity_ceiling: str
    purposes: list[str] = Field(default_factory=list, max_length=10)
    expires_at: datetime | None = None
    idempotency_key: str = Field(..., min_length=1, max_length=300)
    trace_id: str = Field(..., min_length=1, max_length=300)

    def request(self) -> CompanyKnowledgePermissionGrantInput:
        return CompanyKnowledgePermissionGrantInput(
            principal_type=self.principal_type,
            principal_id=self.principal_id,
            principal_key=self.principal_key,
            resource_type=self.resource_type,
            resource_id=self.resource_id,
            resource_key=self.resource_key,
            actions=tuple(self.actions),
            effect=self.effect,
            sensitivity_ceiling=self.sensitivity_ceiling,
            purposes=tuple(self.purposes),
            expires_at=self.expires_at,
            idempotency_key=self.idempotency_key,
        )


class CompanyPermissionRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=10000)
    trace_id: str = Field(..., min_length=1, max_length=300)


class PersonalPromotionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: uuid.UUID
    proposed_namespace: str = Field(..., min_length=1, max_length=300)
    purpose: str = Field(..., min_length=1, max_length=1000)
    risk_level: str = "normal"
    title: str | None = Field(None, max_length=300)
    attest_scope_change: Literal[True]
    idempotency_key: str = Field(..., min_length=1, max_length=200)
    trace_id: str = Field(..., min_length=1, max_length=300)

    def request(self) -> PersonalPromotionIntakeRequest:
        return PersonalPromotionIntakeRequest(**self.model_dump())


class LegacyPromotionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: str = Field(..., min_length=1, max_length=1000)
    expected_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    proposed_namespace: str = Field(..., min_length=1, max_length=300)
    proposed_sensitivity: str
    purpose: str = Field(..., min_length=1, max_length=1000)
    risk_level: str = "normal"
    title: str | None = Field(None, max_length=300)
    attest_scope_change: Literal[True]
    idempotency_key: str = Field(..., min_length=1, max_length=200)
    trace_id: str = Field(..., min_length=1, max_length=300)

    def request(self) -> LegacyPromotionIntakeRequest:
        return LegacyPromotionIntakeRequest(**self.model_dump())


class PromotionRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(..., min_length=1, max_length=300)


class CompanySearchFilters(BaseModel):
    namespaces: list[str] = Field(default_factory=list, max_length=50)
    sensitivities: list[str] = Field(default_factory=list, max_length=4)
    publication_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class CompanySearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    filters: CompanySearchFilters = Field(default_factory=CompanySearchFilters)
    limit: int = Field(10, ge=1, le=50)


class OntologyPackageInstallCreate(BaseModel):
    package_key: str = Field(..., min_length=1, max_length=240)
    version: str = Field(..., min_length=1, max_length=120)
    idempotency_key: str = Field(..., min_length=1, max_length=300)
    trace_id: str = Field(..., min_length=1, max_length=300)


class OntologyActivationCreate(BaseModel):
    installation_id: uuid.UUID
    namespace: str = Field(..., min_length=1, max_length=300)
    configuration: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(..., min_length=1, max_length=300)
    trace_id: str = Field(..., min_length=1, max_length=300)


class OntologyActivationDryRun(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=300)
    trace_id: str = Field(..., min_length=1, max_length=300)


class OntologyReleasePublish(BaseModel):
    valid_from: datetime
    valid_until: datetime | None = None
    trace_id: str = Field(..., min_length=1, max_length=300)


class OntologyReleaseRetire(BaseModel):
    reason: str = Field(..., min_length=1, max_length=10000)
    trace_id: str = Field(..., min_length=1, max_length=300)


class OntologyReleaseRestore(OntologyReleaseRetire):
    approved_proposal_id: uuid.UUID
    valid_from: datetime


class OntologyQueryBody(BaseModel):
    namespaces: list[str] = Field(default_factory=list, max_length=50)
    query_ref: str | None = Field(None, max_length=500)
    query_input: dict[str, Any] = Field(default_factory=dict)
    object_type_refs: list[str] = Field(default_factory=list, max_length=100)
    object_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    limit: int = Field(50, ge=1, le=200)
    include_facts: bool = True
    include_links: bool = True


class OntologyActionSimulationBody(BaseModel):
    proposed_input: dict[str, Any]
    namespace: str | None = Field(None, max_length=300)


def _require_legacy_promotion_admin(current_user: User) -> None:
    if str(current_user.role) not in {"org_admin", "platform_admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="legacy_company_promotion_requires_tenant_admin",
        )


@router.get("/promotion-intakes")
async def list_company_knowledge_promotion_intakes(
    kind: str | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    rows = await _call(
        _promotion_service().list_intakes(
            db,
            principal=_principal(current_user, target_tenant),
            kind=kind,
            limit=limit,
        )
    )
    return {"intakes": [_payload(row) for row in rows]}


@router.post("/promotion-intakes/personal", status_code=status.HTTP_202_ACCEPTED)
async def create_personal_company_knowledge_promotion(
    body: PersonalPromotionCreate,
    background_tasks: BackgroundTasks,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    job = await _call(
        _promotion_service().queue_personal_promotion(
            db,
            principal=_principal(current_user, target_tenant),
            request=body.request(),
        )
    )
    await db.commit()
    _schedule_import_processing(
        background_tasks,
        tenant_id=target_tenant,
        job_id=job.id,
    )
    return {
        "intake_id": str(job.id),
        "status": "queued",
        "recovery": "automatic",
    }


@router.get("/promotion-intakes/legacy-candidates")
async def list_legacy_company_knowledge_promotion_candidates(
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.legacy_company_files import (
        LegacyCompanyFilesChangedError,
        LegacyCompanyFilesUnavailableError,
        scan_legacy_company_files,
    )

    _require_legacy_promotion_admin(current_user)
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    try:
        snapshot = await anyio.to_thread.run_sync(
            scan_legacy_company_files,
            Path(get_settings().AGENT_DATA_DIR) / f"enterprise_info_{target_tenant}",
        )
    except LegacyCompanyFilesChangedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="legacy_company_promotion_source_changed",
        ) from exc
    except LegacyCompanyFilesUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="legacy_company_promotion_source_unavailable",
        ) from exc
    return {
        "candidates": [
            {
                "relative_path": item.relative_path,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
            }
            for item in snapshot.files
        ],
        "excluded_symlink_count": snapshot.excluded_symlink_count,
    }


@router.post("/promotion-intakes/legacy", status_code=status.HTTP_202_ACCEPTED)
async def create_legacy_company_knowledge_promotion(
    body: LegacyPromotionCreate,
    background_tasks: BackgroundTasks,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_legacy_promotion_admin(current_user)
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    job = await _call(
        _promotion_service().queue_legacy_promotion(
            db,
            principal=_principal(current_user, target_tenant),
            company_dir=Path(get_settings().AGENT_DATA_DIR) / f"enterprise_info_{target_tenant}",
            request=body.request(),
        )
    )
    await db.commit()
    _schedule_import_processing(
        background_tasks,
        tenant_id=target_tenant,
        job_id=job.id,
    )
    return {
        "intake_id": str(job.id),
        "status": "queued",
        "recovery": "automatic",
    }


@router.get("/promotion-intakes/{job_id}")
async def get_company_knowledge_promotion_intake(
    job_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    view = await _call(
        _promotion_service().get_intake(
            db,
            principal=_principal(current_user, target_tenant),
            job_id=job_id,
        )
    )
    return _payload(view)


@router.get("/promotion-intakes/{job_id}/candidate")
async def get_company_knowledge_promotion_candidate(
    job_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    candidate = await _call(
        _promotion_service().get_candidate(
            db,
            principal=_principal(current_user, target_tenant),
            job_id=job_id,
        )
    )
    return _payload(candidate)


@router.post("/promotion-intakes/{job_id}/retry")
async def retry_company_knowledge_promotion_intake(
    job_id: uuid.UUID,
    body: PromotionRetry,
    background_tasks: BackgroundTasks,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    view = await _call(
        _promotion_service().retry_intake(
            db,
            principal=_principal(current_user, target_tenant),
            job_id=job_id,
            trace_id=body.trace_id,
        )
    )
    await db.commit()
    _schedule_import_processing(
        background_tasks,
        tenant_id=target_tenant,
        job_id=job_id,
    )
    return _payload(view)


@router.post("/search")
async def search_company_knowledge(
    body: CompanySearchRequest,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await _call(
        _gateway().search(
            db,
            principal=_principal(current_user, target_tenant),
            request=CompanyKnowledgeSearchRequest(
                query=body.query,
                filters=body.filters.model_dump(mode="json"),
                limit=body.limit,
                trace_id=f"company-kb-api-search:{uuid.uuid4()}",
            ),
        )
    )
    await db.commit()
    return result.as_dict()


@router.get("/documents")
async def list_company_knowledge_documents(
    tenant_id: uuid.UUID | None = Query(None),
    namespace: list[str] = Query(default=[]),
    sensitivity: list[str] = Query(default=[]),
    limit: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await _call(
        _gateway().list_documents(
            db,
            principal=_principal(current_user, target_tenant),
            request=CompanyKnowledgeDocumentListRequest(
                filters={
                    "namespaces": namespace,
                    "sensitivities": sensitivity,
                },
                limit=limit,
                trace_id=f"company-kb-api-documents:{uuid.uuid4()}",
            ),
        )
    )
    await db.commit()
    return result.as_dict()


@router.get("/documents/{document_id}")
async def read_company_knowledge_document(
    document_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    publication_id: uuid.UUID | None = Query(None),
    segment_id: list[uuid.UUID] = Query(default=[]),
    max_chars: int = Query(20_000, ge=1, le=100_000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await _call(
        _gateway().read(
            db,
            principal=_principal(current_user, target_tenant),
            request=CompanyKnowledgeReadRequest(
                document_id=document_id,
                publication_id=publication_id,
                segment_ids=tuple(segment_id),
                max_chars=max_chars,
                trace_id=f"company-kb-api-read:{uuid.uuid4()}",
            ),
        )
    )
    await db.commit()
    return result.as_dict()


@router.get("/evidence/{evidence_id}")
async def explain_company_knowledge_evidence(
    evidence_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await _call(
        _gateway().explain_source(
            db,
            principal=_principal(current_user, target_tenant),
            request=CompanyKnowledgeSourceExplainRequest(
                evidence_id=evidence_id,
                trace_id=f"company-kb-api-evidence:{uuid.uuid4()}",
            ),
        )
    )
    await db.commit()
    return result.as_dict()


@router.get("/capabilities")
async def get_company_knowledge_capabilities(
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    return {
        "schema": "hive.company_knowledge_capabilities.v1",
        "tenant_id": str(target_tenant),
        "baseline_search": "postgres_fts",
        "external_provider_required": False,
        "external_provider_status": "unconfigured",
        "retrieval": {
            "active_publications_only": True,
            "fresh_permission_per_result": True,
            "fresh_source_acl": True,
            "complete_evidence_required": True,
            "pointer_only_transcript_replay": True,
        },
        "agent_tools": [
            "search_company_kb",
            "read_company_kb",
            "propose_company_kb_update",
            "explain_company_kb_source",
        ],
    }


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
    rows = await _call(
        _permission_service().list_review_queue(
            db,
            principal=_principal(current_user, target_tenant),
            status=proposal_status,
            limit=limit,
        )
    )
    return {"proposals": rows}


@router.get("/proposals/{proposal_id}")
async def get_proposal(
    proposal_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    row = await _call(
        _service().get_proposal_for_review(
            db,
            principal=_principal(current_user, target_tenant),
            proposal_id=proposal_id,
        )
    )
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


@router.post("/proposals/{proposal_id}/materialize")
async def materialize_proposal(
    proposal_id: uuid.UUID,
    body: ProposalMaterialize,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    proposal = await _call(
        _service().materialize_proposal(
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
            request=body.request(reviewer_role=str(current_user.role)),
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


@router.get("/publications")
async def list_company_knowledge_publication_lifecycle(
    tenant_id: uuid.UUID | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    publications = await _call(
        _service().list_publication_lifecycle(
            db,
            principal=_principal(current_user, target_tenant),
            limit=limit,
        )
    )
    await db.commit()
    return {"publications": publications}


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


@router.get("/permissions")
async def list_company_knowledge_permissions(
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    permissions = await _call(
        _permission_service().list_permissions(
            db,
            principal=_principal(current_user, target_tenant),
        )
    )
    return {"permissions": permissions}


@router.post("/permissions")
async def grant_company_knowledge_permission(
    body: CompanyPermissionGrant,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    permission = await _call(
        _permission_service().grant_permission(
            db,
            principal=_principal(current_user, target_tenant),
            request=body.request(),
            trace_id=body.trace_id,
        )
    )
    await db.commit()
    return permission


@router.post("/permissions/{permission_id}/revoke")
async def revoke_company_knowledge_permission(
    permission_id: uuid.UUID,
    body: CompanyPermissionRevoke,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    receipt = await _call(
        _permission_service().revoke_permission(
            db,
            principal=_principal(current_user, target_tenant),
            permission_id=permission_id,
            reason=body.reason,
            trace_id=body.trace_id,
        )
    )
    await db.commit()
    return receipt


@router.get("/ontology/packages")
async def list_company_ontology_packages(
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    del target_tenant
    catalog = load_builtin_ontology_catalog()
    return {
        "schema": "hive.company_ontology_package_catalog.v1",
        "packages": [
            {
                "package_key": bundle.manifest.package_key,
                "display_name": bundle.manifest.display_name,
                "description": bundle.manifest.description,
                "publisher": bundle.manifest.publisher,
                "version": bundle.manifest.version,
                "namespaces": list(bundle.manifest.namespaces),
                "content_hash": bundle.content_hash,
                "signature_key_ref": bundle.signature.key_ref,
                "signature_valid": True,
                "declarative_only": True,
            }
            for bundle in catalog.all()
        ],
    }


@router.post("/ontology/package-installations")
async def install_company_ontology_package(
    body: OntologyPackageInstallCreate,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    installation = await _call(
        _ontology_service().install_package(
            db,
            principal=_principal(current_user, target_tenant),
            request=OntologyPackageInstallRequest(
                package_key=body.package_key,
                version=body.version,
                idempotency_key=body.idempotency_key,
                trace_id=body.trace_id,
            ),
        )
    )
    await db.commit()
    return _payload(installation)


@router.get("/ontology/package-installations")
async def list_company_ontology_installations(
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    principal = _principal(current_user, target_tenant)
    rows = (
        await db.execute(
            select(
                CompanyOntologyPackageInstallation,
                CompanyOntologyPackageVersion,
                CompanyOntologyPackage,
            )
            .join(
                CompanyOntologyPackageVersion,
                CompanyOntologyPackageVersion.id == CompanyOntologyPackageInstallation.package_version_id,
            )
            .join(
                CompanyOntologyPackage,
                CompanyOntologyPackage.id == CompanyOntologyPackageVersion.package_id,
            )
            .where(
                CompanyOntologyPackageInstallation.tenant_id == target_tenant,
                CompanyOntologyPackageVersion.tenant_id == target_tenant,
                CompanyOntologyPackage.tenant_id == target_tenant,
            )
            .order_by(
                CompanyOntologyPackage.package_key,
                CompanyOntologyPackageVersion.version,
            )
        )
    ).all()
    visible: list[dict[str, Any]] = []
    for installation, version, package in rows:
        namespaces = list(version.namespaces_json or [])
        if not namespaces:
            continue
        await _require_ontology_discover(
            db,
            principal=principal,
            namespace=str(namespaces[0]),
        )
        visible.append(
            {
                "installation": _payload(installation),
                "package_key": package.package_key,
                "display_name": package.display_name,
                "version": version.version,
                "content_hash": version.content_hash,
                "namespaces": namespaces,
            }
        )
    return {"installations": visible}


@router.get("/ontology/package-installations/{installation_id}")
async def get_company_ontology_installation(
    installation_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    row = (
        await db.execute(
            select(
                CompanyOntologyPackageInstallation,
                CompanyOntologyPackageVersion,
                CompanyOntologyPackage,
            )
            .join(
                CompanyOntologyPackageVersion,
                CompanyOntologyPackageVersion.id == CompanyOntologyPackageInstallation.package_version_id,
            )
            .join(
                CompanyOntologyPackage,
                CompanyOntologyPackage.id == CompanyOntologyPackageVersion.package_id,
            )
            .where(
                CompanyOntologyPackageInstallation.id == installation_id,
                CompanyOntologyPackageInstallation.tenant_id == target_tenant,
                CompanyOntologyPackageVersion.tenant_id == target_tenant,
                CompanyOntologyPackage.tenant_id == target_tenant,
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="company_ontology_installation_not_found")
    installation, version, package = row
    namespaces = list(version.namespaces_json or [])
    await _require_ontology_discover(
        db,
        principal=_principal(current_user, target_tenant),
        namespace=str(namespaces[0]) if namespaces else package.package_key,
    )
    return {
        "installation": _payload(installation),
        "package_key": package.package_key,
        "display_name": package.display_name,
        "version": version.version,
        "content_hash": version.content_hash,
        "namespaces": namespaces,
    }


@router.post("/ontology/activations")
async def create_company_ontology_activation(
    body: OntologyActivationCreate,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    activation = await _call(
        _ontology_service().create_activation(
            db,
            principal=_principal(current_user, target_tenant),
            request=OntologyActivationRequest(
                installation_id=body.installation_id,
                namespace=body.namespace,
                configuration=body.configuration,
                idempotency_key=body.idempotency_key,
                trace_id=body.trace_id,
            ),
        )
    )
    await db.commit()
    return _payload(activation)


@router.get("/ontology/activations")
async def list_company_ontology_activations(
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    principal = _principal(current_user, target_tenant)
    rows = (
        (
            await db.execute(
                select(CompanyOntologyActivation)
                .where(CompanyOntologyActivation.tenant_id == target_tenant)
                .order_by(
                    CompanyOntologyActivation.namespace,
                    CompanyOntologyActivation.activation_version.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    visible: list[dict[str, Any]] = []
    for row in rows:
        await _require_ontology_discover(
            db,
            principal=principal,
            namespace=row.namespace,
        )
        visible.append(_payload(row))
    return {"activations": visible}


@router.post("/ontology/activations/{activation_id}/dry-run")
async def dry_run_company_ontology_activation(
    activation_id: uuid.UUID,
    body: OntologyActivationDryRun,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    activation = await _call(
        _ontology_service().dry_run_activation(
            db,
            principal=_principal(current_user, target_tenant),
            activation_id=activation_id,
            idempotency_key=body.idempotency_key,
            trace_id=body.trace_id,
        )
    )
    await db.commit()
    return _payload(activation)


@router.get("/ontology/curation-runs")
async def list_company_ontology_curation_runs(
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    principal = _principal(current_user, target_tenant)
    rows = (
        (
            await db.execute(
                select(CompanyOntologyCurationRun)
                .where(CompanyOntologyCurationRun.tenant_id == target_tenant)
                .order_by(CompanyOntologyCurationRun.created_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        activation = await db.get(CompanyOntologyActivation, row.activation_id)
        if activation is None or activation.tenant_id != target_tenant:
            continue
        await _require_ontology_discover(
            db,
            principal=principal,
            namespace=activation.namespace,
        )
        result.append(
            {
                "id": str(row.id),
                "activation_id": str(row.activation_id),
                "baseline_release_id": (str(row.baseline_release_id) if row.baseline_release_id else None),
                "candidate_patch_ref": row.candidate_patch_ref,
                "candidate_patch_hash": row.candidate_patch_hash,
                "coverage": dict(row.coverage_ledger_json or {}),
                "conflicts": dict(row.conflict_ledger_json or {}),
                "unresolved_questions": list(row.unresolved_questions_json or []),
                "acceptance": dict(row.acceptance_result_json or {}),
                "status": row.status,
                "error_code": row.error_code,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
    return {"curation_runs": result}


@router.get("/ontology/curation-runs/{run_id}")
async def get_company_ontology_curation_run(
    run_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    row = (
        await db.execute(
            select(CompanyOntologyCurationRun).where(
                CompanyOntologyCurationRun.id == run_id,
                CompanyOntologyCurationRun.tenant_id == target_tenant,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="company_ontology_curation_run_not_found")
    activation = await db.get(CompanyOntologyActivation, row.activation_id)
    if activation is None or activation.tenant_id != target_tenant:
        raise HTTPException(status_code=404, detail="company_ontology_curation_run_not_found")
    await _require_ontology_discover(
        db,
        principal=_principal(current_user, target_tenant),
        namespace=activation.namespace,
    )
    return {
        "id": str(row.id),
        "activation_id": str(row.activation_id),
        "baseline_release_id": (str(row.baseline_release_id) if row.baseline_release_id else None),
        "candidate_patch_ref": row.candidate_patch_ref,
        "candidate_patch_hash": row.candidate_patch_hash,
        "coverage": dict(row.coverage_ledger_json or {}),
        "conflicts": dict(row.conflict_ledger_json or {}),
        "unresolved_questions": list(row.unresolved_questions_json or []),
        "acceptance": dict(row.acceptance_result_json or {}),
        "status": row.status,
        "error_code": row.error_code,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.post("/ontology/curation-runs/{run_id}/publish")
async def publish_company_ontology_curation_run(
    run_id: uuid.UUID,
    body: OntologyReleasePublish,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    release = await _call(
        _ontology_service().publish_curation_run(
            db,
            principal=_principal(current_user, target_tenant),
            run_id=run_id,
            valid_from=body.valid_from,
            valid_until=body.valid_until,
            trace_id=body.trace_id,
        )
    )
    await db.commit()
    return _payload(release)


@router.post("/ontology/query")
async def query_company_ontology(
    body: OntologyQueryBody,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await _call(
        _ontology_gateway().query(
            db,
            principal=_principal(current_user, target_tenant),
            request=OntologyQueryRequest(
                namespaces=tuple(body.namespaces),
                query_ref=body.query_ref,
                query_input=body.query_input,
                object_type_refs=tuple(body.object_type_refs),
                object_ids=tuple(body.object_ids),
                limit=body.limit,
                include_facts=body.include_facts,
                include_links=body.include_links,
                trace_id=f"company-ontology-api-query:{uuid.uuid4()}",
            ),
        )
    )
    await db.commit()
    return result.as_dict()


@router.get("/ontology/types")
async def list_company_ontology_types(
    tenant_id: uuid.UUID | None = Query(None),
    namespace: list[str] = Query(default=[]),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await _call(
        _ontology_gateway().list_types(
            db,
            principal=_principal(current_user, target_tenant),
            namespaces=tuple(namespace),
            trace_id=f"company-ontology-api-types:{uuid.uuid4()}",
        )
    )
    await db.commit()
    return result.as_dict()


@router.get("/ontology/objects")
async def list_company_ontology_objects(
    tenant_id: uuid.UUID | None = Query(None),
    namespace: list[str] = Query(default=[]),
    object_type_ref: list[str] = Query(default=[]),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await _call(
        _ontology_gateway().query(
            db,
            principal=_principal(current_user, target_tenant),
            request=OntologyQueryRequest(
                namespaces=tuple(namespace),
                object_type_refs=tuple(object_type_ref),
                limit=limit,
                trace_id=f"company-ontology-api-objects:{uuid.uuid4()}",
            ),
        )
    )
    await db.commit()
    return result.as_dict()


@router.get("/ontology/objects/{object_id}")
async def get_company_ontology_object(
    object_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await _call(
        _ontology_gateway().get_object(
            db,
            principal=_principal(current_user, target_tenant),
            request=OntologyObjectReadRequest(
                object_id=object_id,
                trace_id=f"company-ontology-api-object:{uuid.uuid4()}",
            ),
        )
    )
    await db.commit()
    return result.as_dict()


@router.get("/ontology/facts/{assertion_id}/evidence")
async def explain_company_ontology_fact(
    assertion_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await _call(
        _ontology_gateway().explain_fact(
            db,
            principal=_principal(current_user, target_tenant),
            request=OntologyFactExplainRequest(
                assertion_id=assertion_id,
                trace_id=f"company-ontology-api-fact:{uuid.uuid4()}",
            ),
        )
    )
    await db.commit()
    return result.as_dict()


@router.post("/ontology/actions/{action_type_ref}/simulate")
async def simulate_company_ontology_action(
    action_type_ref: str,
    body: OntologyActionSimulationBody,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await _call(
        _ontology_gateway().simulate_action(
            db,
            principal=_principal(current_user, target_tenant),
            request=OntologyActionSimulationRequest(
                action_type_ref=action_type_ref,
                proposed_input=body.proposed_input,
                namespace=body.namespace,
                trace_id=f"company-ontology-api-simulate:{uuid.uuid4()}",
            ),
        )
    )
    await db.commit()
    return result.as_dict()


@router.get("/ontology/releases")
async def list_company_ontology_releases(
    tenant_id: uuid.UUID | None = Query(None),
    namespace: list[str] = Query(default=[]),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    statement = select(CompanyOntologyRelease).where(CompanyOntologyRelease.tenant_id == target_tenant)
    if namespace:
        statement = statement.where(CompanyOntologyRelease.namespace.in_(namespace))
    rows = (
        (
            await db.execute(
                statement.order_by(
                    CompanyOntologyRelease.namespace,
                    CompanyOntologyRelease.version.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    principal = _principal(current_user, target_tenant)
    visible: list[dict[str, Any]] = []
    for row in rows:
        await _require_ontology_discover(
            db,
            principal=principal,
            namespace=row.namespace,
        )
        visible.append(_payload(row))
    return {"releases": visible}


@router.get("/ontology/releases/{release_id}")
async def get_company_ontology_release(
    release_id: uuid.UUID,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    release = (
        await db.execute(
            select(CompanyOntologyRelease).where(
                CompanyOntologyRelease.id == release_id,
                CompanyOntologyRelease.tenant_id == target_tenant,
            )
        )
    ).scalar_one_or_none()
    if release is None:
        raise HTTPException(status_code=404, detail="company_ontology_release_not_found")
    await _require_ontology_discover(
        db,
        principal=_principal(current_user, target_tenant),
        namespace=release.namespace,
    )
    return _payload(release)


@router.post("/ontology/releases/{release_id}/retire")
async def retire_company_ontology_release(
    release_id: uuid.UUID,
    body: OntologyReleaseRetire,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    release = await _call(
        _ontology_service().retire_release(
            db,
            principal=_principal(current_user, target_tenant),
            release_id=release_id,
            request=OntologyReleaseLifecycleRequest(
                reason=body.reason,
                trace_id=body.trace_id,
            ),
        )
    )
    await db.commit()
    return _payload(release)


@router.post("/ontology/releases/{release_id}/restore")
async def restore_company_ontology_release(
    release_id: uuid.UUID,
    body: OntologyReleaseRestore,
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    release = await _call(
        _ontology_service().restore_release(
            db,
            principal=_principal(current_user, target_tenant),
            release_id=release_id,
            request=OntologyReleaseLifecycleRequest(
                reason=body.reason,
                trace_id=body.trace_id,
                approved_proposal_id=body.approved_proposal_id,
                valid_from=body.valid_from,
            ),
        )
    )
    await db.commit()
    return _payload(release)


@router.get("/ontology/capabilities")
async def get_company_ontology_capabilities(
    tenant_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_tenant = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    del target_tenant
    engine = await ReferenceOntologyEngine().capability_status()
    catalog = load_builtin_ontology_catalog()
    return {
        "schema": "hive.company_ontology_capabilities.v1",
        "engine": engine,
        "domain_packs": [
            {
                "package_key": key,
                "versions": list(catalog.versions(key)),
            }
            for key in catalog.package_keys
        ],
        "agent_tools": [
            "query_company_ontology",
            "get_company_object",
            "explain_company_fact",
            "propose_ontology_change",
            "simulate_company_action",
        ],
        "administrative_actions_agent_exposed": False,
        "release_authority": "hive_postgresql",
        "projection_rebuildable": True,
    }


__all__ = ["router"]
