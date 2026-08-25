"""Governed Company Knowledge source, ingest, review, and publication service.

The service owns authority transitions and durable evidence. It does not make
semantic decisions: source content and proposal patches are supplied by the
authenticated caller, while policy, ACL, lifecycle, idempotency, and recovery
remain deterministic platform responsibilities.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select, text

from app.config import get_settings
from app.database import tenant_scoped_session
from app.models.company_knowledge import (
    CompanyKnowledgeEvidence,
    CompanyKnowledgeImportJob,
    CompanyKnowledgeProposal,
    CompanyKnowledgePublication,
    CompanyKnowledgeReview,
    CompanyKnowledgeSource,
    CompanyKnowledgeSourceContract,
)
from app.models.knowledge import KnowledgeDocument, KnowledgeSegment
from app.services.company_knowledge_contracts import (
    CanonicalEvidenceInput,
    CompanyKnowledgePromotionHandoff,
    SourceContractInput,
    build_canonical_evidence_envelope,
    company_knowledge_artifact_path,
    company_knowledge_proposal_requires_materialization,
    compute_source_contract_hash,
    default_company_knowledge_review_policy,
    evaluate_company_review_set,
    next_company_proposal_status,
    validate_company_knowledge_promotion_handoff,
    validate_source_contract,
)
from app.services.company_knowledge_evidence import (
    CompanyKnowledgeEventInput,
    append_company_knowledge_event,
    append_company_knowledge_event_with_outbox,
)
from app.services.company_knowledge_permissions import (
    CompanyKnowledgePrincipal,
    CompanyKnowledgeResource,
    resolve_company_knowledge_permission,
)
from app.services.personal_knowledge_ingest import (
    _safe_filename,
    clean_title,
    normalize_markdown,
    segment_markdown,
)
from app.services.privacy_layer import canonicalize_sensitivity, sensitivity_rank


_EVIDENCE_KINDS = frozenset(
    {"document", "structured_record", "event", "living_object_revision", "external_immutable_ref"}
)
# Direct file import accepts exactly the formats with real conversion
# vertical proof (PDF, DOCX, Markdown, plain text).
_DIRECT_IMPORT_EXTENSIONS = frozenset({".pdf", ".docx", ".md", ".markdown", ".txt"})
_PROPOSAL_KINDS = frozenset(
    {"knowledge", "ontology", "combined", "personal_promotion", "living_object", "legacy_import"}
)
_RISK_LEVELS = frozenset({"normal", "high", "critical"})
_REVIEW_DECISIONS = frozenset({"approve", "reject", "request_changes", "abstain"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _company_evidence_request_payload(request: Any) -> dict[str, Any]:
    payload = _jsonable(asdict(request))
    if getattr(request, "promotion_handoff", None) is None:
        # Preserve the pre-promotion hash/JSON contract for ordinary imports.
        payload.pop("promotion_handoff", None)
    return payload


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_relative_to(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    resolved.relative_to(root.expanduser().resolve())
    return resolved


def _event_input(
    *,
    principal: CompanyKnowledgePrincipal,
    event_type: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    resource_version: int | None,
    source_refs: tuple[str, ...],
    source_hash: str | None,
    policy_snapshot: dict[str, Any],
    trace_id: str,
    idempotency_key: str,
    outcome: str,
    payload: dict[str, Any],
    occurred_at: datetime | None = None,
) -> CompanyKnowledgeEventInput:
    return CompanyKnowledgeEventInput(
        tenant_id=principal.tenant_id,
        event_type=event_type,
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        accountable_user_id=principal.accountable_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_version=resource_version,
        source_refs=source_refs,
        source_hash=source_hash,
        policy_snapshot=policy_snapshot,
        trace_id=trace_id,
        request_id=None,
        idempotency_key=idempotency_key,
        outcome=outcome,
        payload=payload,
        occurred_at=occurred_at or _utcnow(),
    )


@dataclass(frozen=True, slots=True)
class CompanyEvidenceIngestRequest:
    source_contract_id: uuid.UUID
    source_contract_version: int
    evidence_kind: str
    source_item_id: str
    source_revision: str
    title: str
    markdown: str | None
    typed_payload: dict[str, Any] | None
    external_artifact_ref: str | None
    schema_ref: str | None
    source_acl_snapshot: dict[str, Any]
    proposed_namespace: str
    proposed_sensitivity: str
    occurred_at: datetime | None
    effective_from: datetime | None
    effective_until: datetime | None
    observed_at: datetime
    cursor: dict[str, Any]
    sequence: str | None
    coverage_ledger: dict[str, Any]
    purpose: str
    idempotency_key: str
    trace_id: str
    promotion_handoff: CompanyKnowledgePromotionHandoff | None = None


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeProposalRequest:
    proposal_kind: str
    source_id: uuid.UUID | None
    source_document_id: uuid.UUID | None
    source_revision_ref: str | None
    baseline_publication_id: uuid.UUID | None
    baseline_version: int | None
    proposed_patch: dict[str, Any]
    proposed_namespace: str
    proposed_sensitivity: str
    source_refs: tuple[str, ...]
    source_coverage: dict[str, Any]
    conflict_candidates: tuple[dict[str, Any], ...]
    ontology_mapping: dict[str, Any]
    risk_level: str
    required_review_policy: dict[str, Any]
    idempotency_key: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeReviewRequest:
    decision: str
    reviewer_role: str
    reason: str
    evidence_refs: tuple[str, ...]
    policy_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeMaterializationRequest:
    title: str
    markdown: str
    expected_proposed_content_hash: str
    attest_candidate_applied: bool
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeImportRecoverySummary:
    attempted: int
    completed: int
    failed: int
    skipped: int
    job_refs: tuple[tuple[uuid.UUID, uuid.UUID], ...]


class CompanyKnowledgeImportError(Exception):
    """Typed import-pipeline failure carrying one exact machine code.

    The code is the user-facing error contract; raw exception prose never
    becomes UI state.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CompanyKnowledgeJobConflict(Exception):
    """Typed lifecycle conflict (cancel/retry/preview/proposal) with an exact machine code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeImportJobSummary:
    """User-consumable read model for one import job (raw row stays operator evidence)."""

    job_id: uuid.UUID
    status: str
    lifecycle_status: str
    attempt_count: int
    max_attempts: int
    terminal: bool
    retryable: bool
    cancellable: bool
    error_code: str | None
    title: str
    source_filename: str | None
    namespace: str
    sensitivity: str
    source_id: uuid.UUID | None
    evidence_id: uuid.UUID | None
    document_id: uuid.UUID | None
    proposal_id: uuid.UUID | None
    idempotency_key: str
    cancelled_at: str | None
    created_at: Any
    updated_at: Any
    completed_at: Any


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeImportPreviewSegment:
    segment_id: uuid.UUID
    position: int
    heading_path: list[str]
    content: str
    token_count: int


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeImportPreview:
    """Pre-publication segment preview for a completed import (admin-only)."""

    job_id: uuid.UUID
    document_id: uuid.UUID
    evidence_id: uuid.UUID | None
    source_id: uuid.UUID | None
    proposal_id: uuid.UUID | None
    title: str
    namespace: str
    sensitivity: str
    segments: list[CompanyKnowledgeImportPreviewSegment]


_JOB_LIFECYCLE_BY_RAW_STATUS = {
    "queued": "queued",
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "held": "held",
}

# Permanent failure codes: retrying cannot change the outcome (bad input,
# missing evidence, exhausted budget). Conversion failures and timeouts stay
# retryable; a missing spool is recovered by re-uploading as a new import.
_PERMANENT_IMPORT_ERROR_CODES = frozenset(
    {
        "unsupported_file_type",
        "source_missing",
        "import_payload_invalid",
        "company_knowledge_import_attempts_exhausted",
    }
)


def company_import_job_view(job: Any) -> CompanyKnowledgeImportJobSummary:
    """Derive the lifecycle read model from the durable job row (no schema change)."""
    request = dict(getattr(job, "request_json", {}) or {})
    status = str(getattr(job, "status", "") or "")
    lifecycle_status = _JOB_LIFECYCLE_BY_RAW_STATUS.get(status, status)
    terminal = lifecycle_status not in {"queued", "running"}
    attempt_count = int(getattr(job, "attempt_count", 0) or 0)
    max_attempts = int(getattr(job, "max_attempts", 0) or 0)
    error_code = getattr(job, "last_error_code", None)
    direct_file = dict(request.get("direct_file_import") or {})
    return CompanyKnowledgeImportJobSummary(
        job_id=getattr(job, "id", None),
        status=status,
        lifecycle_status=lifecycle_status,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        terminal=terminal,
        retryable=(
            lifecycle_status in {"failed", "cancelled"}
            and attempt_count < max_attempts
            and error_code not in _PERMANENT_IMPORT_ERROR_CODES
        ),
        cancellable=lifecycle_status == "queued",
        error_code=error_code,
        title=str(request.get("title") or ""),
        source_filename=direct_file.get("source_filename"),
        namespace=str(request.get("proposed_namespace") or ""),
        sensitivity=str(request.get("proposed_sensitivity") or ""),
        source_id=getattr(job, "source_id", None),
        evidence_id=getattr(job, "evidence_id", None),
        document_id=getattr(job, "document_id", None),
        proposal_id=getattr(job, "proposal_id", None),
        idempotency_key=str(getattr(job, "idempotency_key", "") or ""),
        cancelled_at=str(request.get("cancelled_at") or "") or None,
        created_at=getattr(job, "created_at", None),
        updated_at=getattr(job, "updated_at", None),
        completed_at=getattr(job, "completed_at", None),
    )


class CompanyKnowledgeService:
    """Company authority service shared by API handlers and background workers."""

    def __init__(
        self,
        *,
        data_root: str | Path,
        conversion_service: Any | None = None,
        conversion_timeout_seconds: float | None = None,
    ) -> None:
        self._data_root = Path(data_root).expanduser().resolve()
        self._conversion_service_override = conversion_service
        if conversion_timeout_seconds is None:
            conversion_timeout_seconds = float(get_settings().COMPANY_KB_CONVERSION_TIMEOUT_SECONDS)
        self._conversion_timeout_seconds = max(0.001, float(conversion_timeout_seconds))

    @staticmethod
    async def _require_permission(
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        resource: CompanyKnowledgeResource,
        action: str,
    ) -> dict[str, Any]:
        decision = await resolve_company_knowledge_permission(
            session,
            principal=principal,
            resource=resource,
            action=action,  # type: ignore[arg-type]
        )
        if not decision.allowed:
            raise PermissionError(decision.deny_reason_code or "company_knowledge_permission_denied")
        return decision.evidence()

    async def register_source_contract(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        contract_input: SourceContractInput,
        idempotency_key: str,
        trace_id: str,
    ) -> CompanyKnowledgeSourceContract:
        validate_source_contract(contract_input)
        contract_hash = compute_source_contract_hash(contract_input)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {
                "lock_key": (
                    f"company-knowledge-source-contract:{principal.tenant_id}:{contract_input.stable_source_id}"
                )
            },
        )
        resource = CompanyKnowledgeResource(
            tenant_id=principal.tenant_id,
            resource_type="company_knowledge_scope",
            resource_id=principal.tenant_id,
            resource_key=f"tenant:{principal.tenant_id}",
            namespace=contract_input.allowed_namespaces[0],
            sensitivity=contract_input.default_sensitivity,
            source_acl_snapshot_hash=None,
            source_acl=None,
            evidence_access_complete=True,
            publication_status=None,
        )
        policy = await self._require_permission(
            session,
            principal=principal,
            resource=resource,
            action="propose",
        )
        existing = (
            await session.execute(
                select(CompanyKnowledgeSourceContract)
                .where(
                    CompanyKnowledgeSourceContract.tenant_id == principal.tenant_id,
                    CompanyKnowledgeSourceContract.stable_source_id == contract_input.stable_source_id,
                    CompanyKnowledgeSourceContract.contract_hash == contract_hash,
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        current = (
            await session.execute(
                select(CompanyKnowledgeSourceContract)
                .where(
                    CompanyKnowledgeSourceContract.tenant_id == principal.tenant_id,
                    CompanyKnowledgeSourceContract.stable_source_id == contract_input.stable_source_id,
                    CompanyKnowledgeSourceContract.status == "active",
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        version = (
            await session.scalar(
                select(func.coalesce(func.max(CompanyKnowledgeSourceContract.version), 0)).where(
                    CompanyKnowledgeSourceContract.tenant_id == principal.tenant_id,
                    CompanyKnowledgeSourceContract.stable_source_id == contract_input.stable_source_id,
                )
            )
        ) + 1
        now = _utcnow()
        if current is not None:
            current.status = "retired"
            current.retired_at = now
        contract = CompanyKnowledgeSourceContract(
            tenant_id=principal.tenant_id,
            version=version,
            status="active",
            source_kind=contract_input.source_kind,
            provider_kind=contract_input.provider_kind,
            stable_source_id=contract_input.stable_source_id,
            owner_principal_ref=contract_input.owner_principal_ref,
            accountable_steward_ref=contract_input.accountable_steward_ref,
            connection_ref=contract_input.connection_ref,
            schema_ref=contract_input.schema_ref,
            schema_version=contract_input.schema_version,
            identity_keys_json=list(contract_input.identity_keys),
            relation_keys_json=list(contract_input.relation_keys),
            ingest_mode=contract_input.ingest_mode,
            cursor_kind=contract_input.cursor_kind,
            cursor_policy_json=dict(contract_input.cursor_policy),
            watermark_field=contract_input.watermark_field,
            temporal_mapping_json=dict(contract_input.temporal_mapping),
            source_acl_mapping_policy_json=dict(contract_input.source_acl_mapping_policy),
            default_sensitivity=canonicalize_sensitivity(contract_input.default_sensitivity).value,
            export_policy_json=dict(contract_input.export_policy),
            retention_policy_json=dict(contract_input.retention_policy),
            legal_hold_policy_json=dict(contract_input.legal_hold_policy),
            allowed_namespaces_json=list(contract_input.allowed_namespaces),
            precedence_policy_ref=contract_input.precedence_policy_ref,
            acceptance_suite_ref=contract_input.acceptance_suite_ref,
            idempotency_policy_json=dict(contract_input.idempotency_policy),
            contract_hash=contract_hash,
            created_by_user_id=principal.accountable_user_id,
            reviewed_by_json=[],
            effective_from=now,
        )
        session.add(contract)
        await session.flush()
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.source_contract_registered",
                resource_type="source_contract",
                resource_id=contract.id,
                resource_version=contract.version,
                source_refs=(f"company-source-contract://{contract.id}",),
                source_hash=contract.contract_hash,
                policy_snapshot=policy,
                trace_id=trace_id,
                idempotency_key=idempotency_key,
                outcome="active",
                payload={"stable_source_id": contract.stable_source_id, "status": contract.status},
                occurred_at=now,
            ),
        )
        return contract

    async def queue_evidence_import(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: CompanyEvidenceIngestRequest,
    ) -> CompanyKnowledgeImportJob:
        if request.evidence_kind not in _EVIDENCE_KINDS:
            raise ValueError("unsupported_company_evidence_kind")
        if not request.idempotency_key.strip() or not request.source_item_id.strip():
            raise ValueError("source_item_id_and_idempotency_key_required")
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": (f"company-knowledge-import:{principal.tenant_id}:{request.idempotency_key}")},
        )
        contract = (
            await session.execute(
                select(CompanyKnowledgeSourceContract)
                .where(
                    CompanyKnowledgeSourceContract.id == request.source_contract_id,
                    CompanyKnowledgeSourceContract.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if contract is None:
            raise LookupError("company_source_contract_not_found")
        if contract.status != "active" or contract.version != request.source_contract_version:
            raise ValueError("company_source_contract_not_active")
        if request.proposed_namespace not in set(contract.allowed_namespaces_json or []):
            raise PermissionError("company_source_namespace_not_allowed")
        sensitivity = canonicalize_sensitivity(request.proposed_sensitivity).value
        if sensitivity_rank(sensitivity) < sensitivity_rank(contract.default_sensitivity):
            raise ValueError("company_source_declassification_not_allowed")
        if not request.source_acl_snapshot:
            raise ValueError("company_source_acl_snapshot_required")
        source_acl_hash = _hash_json(request.source_acl_snapshot)
        resource = CompanyKnowledgeResource(
            tenant_id=principal.tenant_id,
            resource_type="company_knowledge_namespace",
            resource_id=None,
            resource_key=f"namespace:{request.proposed_namespace}",
            namespace=request.proposed_namespace,
            sensitivity=sensitivity,
            source_acl_snapshot_hash=source_acl_hash,
            source_acl=dict(request.source_acl_snapshot),
            evidence_access_complete=bool(request.coverage_ledger.get("complete")),
            publication_status=None,
        )
        policy = await self._require_permission(
            session,
            principal=principal,
            resource=resource,
            action="propose",
        )

        if request.evidence_kind == "document":
            normalized = normalize_markdown(request.markdown or "")
            if not normalized:
                raise ValueError("document_markdown_required")
            payload = normalized.encode("utf-8")
            suffix = ".md"
        elif request.evidence_kind in {"structured_record", "event"}:
            if request.typed_payload is None or not request.schema_ref:
                raise ValueError("typed_evidence_requires_schema_and_payload")
            payload = _canonical_json(request.typed_payload).encode("utf-8")
            suffix = ".json"
        else:
            if not request.external_artifact_ref:
                raise ValueError("immutable_external_artifact_ref_required")
            payload = request.external_artifact_ref.encode("utf-8")
            suffix = ".txt"
        artifact_hash = hashlib.sha256(payload).hexdigest()
        if request.promotion_handoff is not None:
            if request.evidence_kind != "document":
                raise ValueError("company_knowledge_promotion_requires_document_evidence")
            validate_company_knowledge_promotion_handoff(
                request.promotion_handoff,
                artifact_hash=artifact_hash,
                markdown=normalized,
            )
        request_payload = _company_evidence_request_payload(request)
        request_hash = _hash_json(
            {
                **request_payload,
                "artifact_hash": artifact_hash,
                "source_acl_snapshot_hash": source_acl_hash,
                "principal": principal.evidence(),
            }
        )
        existing = (
            await session.execute(
                select(CompanyKnowledgeImportJob)
                .where(
                    CompanyKnowledgeImportJob.tenant_id == principal.tenant_id,
                    CompanyKnowledgeImportJob.idempotency_key == request.idempotency_key,
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ValueError("company_knowledge_import_idempotency_conflict")
            return existing

        evidence_id = uuid.uuid4()
        artifact_path = company_knowledge_artifact_path(
            self._data_root,
            tenant_id=principal.tenant_id,
            evidence_id=evidence_id,
            content_hash=artifact_hash,
            suffix=suffix,
        )
        _atomic_write(artifact_path, payload)
        serialized_request = {
            **request_payload,
            "evidence_id": str(evidence_id),
            "artifact_ref": str(artifact_path),
            "artifact_hash": artifact_hash,
            "source_acl_snapshot_hash": source_acl_hash,
            "permission_decision": policy,
            "principal": principal.evidence(),
        }

        now = _utcnow()
        job = CompanyKnowledgeImportJob(
            tenant_id=principal.tenant_id,
            source_contract_id=contract.id,
            source_contract_version=contract.version,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            request_json=serialized_request,
            artifact_ref=str(artifact_path),
            artifact_hash=artifact_hash,
            status="queued",
            available_at=now,
            attempt_count=0,
            max_attempts=5,
            created_by_type=principal.actor_type,
            created_by_id=principal.actor_id,
            accountable_user_id=principal.accountable_user_id,
            trace_id=request.trace_id,
        )
        session.add(job)
        await session.flush()
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.import_queued",
                resource_type="import_job",
                resource_id=job.id,
                resource_version=1,
                source_refs=(f"company-source-contract://{contract.id}",),
                source_hash=artifact_hash,
                policy_snapshot=policy,
                trace_id=request.trace_id,
                idempotency_key=f"{request.idempotency_key}:queued",
                outcome="queued",
                payload={"job_id": str(job.id), "artifact_hash": artifact_hash},
                occurred_at=now,
            ),
        )
        return job

    async def queue_direct_file_import(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        source_contract_id: uuid.UUID,
        source_contract_version: int,
        filename: str,
        data: bytes,
        source_mime_type: str | None,
        title: str,
        proposed_namespace: str,
        proposed_sensitivity: str,
        purpose: str,
        source_acl_snapshot: dict[str, Any],
        idempotency_key: str,
        trace_id: str,
    ) -> CompanyKnowledgeImportJob:
        """Queue one admin file upload for worker-side conversion and ingest.

        Only the vertically proven formats are accepted (PDF, DOCX, Markdown,
        plain text); anything else is rejected at the boundary with a typed
        code. The raw source bytes are spooled durably; conversion runs inside
        the worker, never inside the request transaction.
        """
        safe_name = _safe_filename(filename)
        extension = Path(safe_name).suffix.lower()
        if extension not in _DIRECT_IMPORT_EXTENSIONS:
            raise CompanyKnowledgeImportError("unsupported_file_type")
        if not data:
            raise CompanyKnowledgeImportError("import_payload_invalid")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key_required")
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": (f"company-knowledge-import:{principal.tenant_id}:{idempotency_key}")},
        )
        contract = (
            await session.execute(
                select(CompanyKnowledgeSourceContract)
                .where(
                    CompanyKnowledgeSourceContract.id == source_contract_id,
                    CompanyKnowledgeSourceContract.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if contract is None:
            raise LookupError("company_source_contract_not_found")
        if contract.status != "active" or contract.version != int(source_contract_version):
            raise ValueError("company_source_contract_not_active")
        if proposed_namespace not in set(contract.allowed_namespaces_json or []):
            raise PermissionError("company_source_namespace_not_allowed")
        sensitivity = canonicalize_sensitivity(proposed_sensitivity).value
        if sensitivity_rank(sensitivity) < sensitivity_rank(contract.default_sensitivity):
            raise ValueError("company_source_declassification_not_allowed")
        if not source_acl_snapshot:
            raise ValueError("company_source_acl_snapshot_required")
        source_acl_hash = _hash_json(source_acl_snapshot)
        resource = CompanyKnowledgeResource(
            tenant_id=principal.tenant_id,
            resource_type="company_knowledge_namespace",
            resource_id=None,
            resource_key=f"namespace:{proposed_namespace}",
            namespace=proposed_namespace,
            sensitivity=sensitivity,
            source_acl_snapshot_hash=source_acl_hash,
            source_acl=dict(source_acl_snapshot),
            evidence_access_complete=True,
            publication_status=None,
        )
        policy = await self._require_permission(
            session,
            principal=principal,
            resource=resource,
            action="propose",
        )

        artifact_hash = hashlib.sha256(data).hexdigest()
        request_hash = _hash_json(
            {
                "import_kind": "direct_file",
                "source_contract_id": str(contract.id),
                "source_contract_version": contract.version,
                "source_filename": safe_name,
                "artifact_hash": artifact_hash,
                # Every semantic input that changes the persisted outcome or
                # conversion behavior participates in the key; trace/time
                # metadata deliberately does not.
                "title": clean_title(title),
                "purpose": str(purpose or ""),
                "source_mime_type": str(source_mime_type or ""),
                "proposed_namespace": proposed_namespace,
                "proposed_sensitivity": sensitivity,
                "source_acl_snapshot_hash": source_acl_hash,
                "principal": principal.evidence(),
            }
        )
        existing = (
            await session.execute(
                select(CompanyKnowledgeImportJob)
                .where(
                    CompanyKnowledgeImportJob.tenant_id == principal.tenant_id,
                    CompanyKnowledgeImportJob.idempotency_key == idempotency_key,
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ValueError("company_knowledge_import_idempotency_conflict")
            return existing

        evidence_id = uuid.uuid4()
        spool_path = (
            self._data_root
            / "companies"
            / str(principal.tenant_id)
            / "knowledge"
            / "imports"
            / artifact_hash[:2]
            / f"{evidence_id}-{artifact_hash}{extension}"
        )
        _atomic_write(spool_path, data)
        source_revision = artifact_hash[:16]
        serialized_request = {
            "import_kind": "direct_file",
            "direct_file_import": {
                "source_filename": safe_name,
                "source_mime_type": str(source_mime_type or ""),
            },
            "evidence_kind": "document",
            "source_item_id": f"file:{safe_name}",
            "source_revision": source_revision,
            "title": clean_title(title),
            "proposed_namespace": proposed_namespace,
            "proposed_sensitivity": sensitivity,
            "purpose": str(purpose or ""),
            "source_acl_snapshot": dict(source_acl_snapshot),
            "source_acl_snapshot_hash": source_acl_hash,
            "coverage_ledger": {"complete": True, "covered_units": 1, "total_units": 1},
            "permission_decision": policy,
            "principal": principal.evidence(),
            "evidence_id": str(evidence_id),
            "artifact_ref": str(spool_path),
            "artifact_hash": artifact_hash,
            "occurred_at": None,
            "effective_from": None,
            "effective_until": None,
            "observed_at": _utcnow().isoformat(),
            "cursor": {},
            "sequence": None,
        }
        now = _utcnow()
        job = CompanyKnowledgeImportJob(
            tenant_id=principal.tenant_id,
            source_contract_id=contract.id,
            source_contract_version=contract.version,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            request_json=serialized_request,
            artifact_ref=str(spool_path),
            artifact_hash=artifact_hash,
            status="queued",
            available_at=now,
            attempt_count=0,
            max_attempts=5,
            created_by_type=principal.actor_type,
            created_by_id=principal.actor_id,
            accountable_user_id=principal.accountable_user_id,
            trace_id=trace_id,
        )
        session.add(job)
        await session.flush()
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.import_queued",
                resource_type="import_job",
                resource_id=job.id,
                resource_version=1,
                source_refs=(f"company-source-contract://{contract.id}",),
                source_hash=artifact_hash,
                policy_snapshot=policy,
                trace_id=trace_id,
                idempotency_key=f"{idempotency_key}:queued",
                outcome="queued",
                payload={"job_id": str(job.id), "artifact_hash": artifact_hash, "import_kind": "direct_file"},
                occurred_at=now,
            ),
        )
        return job

    async def _convert_direct_file_payload(
        self,
        session: Any,
        *,
        job: CompanyKnowledgeImportJob,
        request: dict[str, Any],
        payload: bytes,
        tenant_id: uuid.UUID,
    ) -> tuple[bytes, Path]:
        """Convert spooled source bytes to canonical Markdown in the worker.

        The blocking converter runs in a killable child process under an
        explicit physical timeout; failures raise typed codes
        (conversion_timeout / conversion_failed). On success the job's
        artifact points at the canonical Markdown and a conversion receipt
        preserves the source hash.
        """
        direct_file = dict(request.get("direct_file_import") or {})
        filename = str(direct_file.get("source_filename") or "upload.bin")
        mime = str(direct_file.get("source_mime_type") or "") or "application/octet-stream"
        converter_override = self._conversion_service_override
        try:
            # The default production path converts in a killable child process
            # so a physical timeout cannot leave an abandoned worker running.
            # An injected converter is test-only DI and keeps the thread path.
            if converter_override is not None:
                conversion = await asyncio.wait_for(
                    asyncio.to_thread(
                        converter_override.convert_bytes,
                        data=payload,
                        filename=filename,
                        workspace_root=self._data_root / "companies" / str(tenant_id) / "knowledge" / "conversion",
                        source_uri=f"company-import://{job.id}/source",
                        source_mime_type=mime,
                        tenant_id=tenant_id,
                        agent_id=None,
                        user_id=job.accountable_user_id,
                        mode="auto",
                        force_refresh=False,
                    ),
                    timeout=self._conversion_timeout_seconds,
                )
            else:
                from app.services.document_conversion import convert_bytes_in_killable_process

                conversion = await convert_bytes_in_killable_process(
                    data=payload,
                    filename=filename,
                    workspace_root=self._data_root / "companies" / str(tenant_id) / "knowledge" / "conversion",
                    timeout_seconds=self._conversion_timeout_seconds,
                    source_uri=f"company-import://{job.id}/source",
                    source_mime_type=mime,
                    tenant_id=tenant_id,
                    agent_id=None,
                    user_id=job.accountable_user_id,
                    mode="auto",
                    force_refresh=False,
                )
        except asyncio.TimeoutError as exc:
            raise CompanyKnowledgeImportError("conversion_timeout") from exc
        except Exception as exc:
            raise CompanyKnowledgeImportError("conversion_failed") from exc
        canonical = normalize_markdown(str(getattr(conversion, "markdown", "") or ""))
        if not canonical:
            raise CompanyKnowledgeImportError("conversion_failed")
        canonical_bytes = canonical.encode("utf-8")
        canonical_hash = hashlib.sha256(canonical_bytes).hexdigest()
        canonical_path = company_knowledge_artifact_path(
            self._data_root,
            tenant_id=tenant_id,
            evidence_id=uuid.UUID(str(request["evidence_id"])),
            content_hash=canonical_hash,
            suffix=".md",
        )
        _atomic_write(canonical_path, canonical_bytes)
        request["conversion_receipt"] = {
            "engine": str(getattr(conversion, "engine", "") or ""),
            "source_sha256": hashlib.sha256(payload).hexdigest(),
            "warnings": list(getattr(conversion, "warnings", ()) or []),
            "converted_at": _utcnow().isoformat(),
        }
        job.artifact_ref = str(canonical_path)
        job.artifact_hash = canonical_hash
        job.request_json = request
        await session.flush()
        return canonical_bytes, canonical_path

    async def _complete_promotion_handoff(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        job: CompanyKnowledgeImportJob,
        request: dict[str, Any],
        source: CompanyKnowledgeSource,
        evidence: CompanyKnowledgeEvidence,
        document: KnowledgeDocument,
        markdown: str,
    ) -> CompanyKnowledgeProposal | None:
        raw_handoff = request.get("promotion_handoff")
        if not isinstance(raw_handoff, dict) or not raw_handoff:
            return None
        if principal.actor_type != "user":
            raise PermissionError("agents_cannot_promote_personal_or_legacy_knowledge")
        handoff = CompanyKnowledgePromotionHandoff(**raw_handoff)
        validate_company_knowledge_promotion_handoff(
            handoff,
            artifact_hash=job.artifact_hash,
            markdown=markdown,
        )
        evidence_ref = f"company-evidence://{evidence.id}"
        proposal = await self.create_proposal(
            session,
            principal=principal,
            request=CompanyKnowledgeProposalRequest(
                proposal_kind=handoff.proposal_kind,
                source_id=source.id,
                source_document_id=document.id,
                source_revision_ref=handoff.source_revision_ref,
                baseline_publication_id=None,
                baseline_version=None,
                proposed_patch={
                    "operation": handoff.proposal_kind,
                    "title": handoff.title,
                    "content_hash": handoff.candidate_content_hash,
                    "content_ref": f"company-import://{job.id}/candidate",
                    "original_source_ref": handoff.original_source_ref,
                    "original_source_label": handoff.original_source_label,
                    "source_revision_ref": handoff.source_revision_ref,
                    "scope_change_attested": True,
                    "reason": handoff.purpose,
                    "conversion_receipt": dict(handoff.conversion_receipt),
                },
                proposed_namespace=handoff.proposed_namespace,
                proposed_sensitivity=handoff.proposed_sensitivity,
                source_refs=(evidence_ref,),
                source_coverage=dict(request["coverage_ledger"]),
                conflict_candidates=(),
                ontology_mapping={},
                risk_level=handoff.risk_level,
                required_review_policy=default_company_knowledge_review_policy(
                    proposed_sensitivity=handoff.proposed_sensitivity,
                    risk_level=handoff.risk_level,
                    created_by_type=principal.actor_type,
                ),
                idempotency_key=handoff.proposal_idempotency_key,
                trace_id=handoff.trace_id,
            ),
        )
        if (
            proposal.source_id != source.id
            or proposal.source_document_id != document.id
            or tuple(proposal.source_refs_json or []) != (evidence_ref,)
        ):
            raise RuntimeError("company_knowledge_promotion_proposal_binding_conflict")
        if proposal.status == "draft":
            proposal = await self.submit_proposal(
                session,
                principal=principal,
                proposal_id=proposal.id,
                expected_state_version=proposal.state_version,
                trace_id=handoff.trace_id,
            )
        elif proposal.status != "submitted":
            raise RuntimeError("company_knowledge_promotion_proposal_state_conflict")
        job.proposal_id = proposal.id
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.promotion_handoff_completed",
                resource_type="import_job",
                resource_id=job.id,
                resource_version=job.attempt_count,
                source_refs=(evidence_ref, handoff.original_source_ref),
                source_hash=handoff.candidate_content_hash,
                policy_snapshot=dict(request["permission_decision"]),
                trace_id=handoff.trace_id,
                idempotency_key=f"{job.idempotency_key}:promotion-handoff",
                outcome="submitted",
                payload={
                    "proposal_id": str(proposal.id),
                    "proposal_kind": proposal.proposal_kind,
                    "status": proposal.status,
                },
            ),
        )
        return proposal

    async def process_import_job(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        session_factory: Any,
    ) -> CompanyKnowledgeImportJob:
        claim_token = uuid.uuid4()
        now = _utcnow()
        async with tenant_scoped_session(
            tenant_id,
            session_factory=session_factory,
            require_tenant=True,
            source="company_knowledge_import_claim",
        ) as session:
            job = (
                await session.execute(
                    select(CompanyKnowledgeImportJob)
                    .where(
                        CompanyKnowledgeImportJob.id == job_id,
                        CompanyKnowledgeImportJob.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                raise LookupError("company_knowledge_import_job_not_found")
            if job.status == "completed":
                return job
            if job.status == "running" and job.claim_expires_at and job.claim_expires_at > now:
                raise RuntimeError("company_knowledge_import_job_already_claimed")
            if job.status in {"held", "cancelled"}:
                raise RuntimeError(f"company_knowledge_import_job_{job.status}")
            if job.attempt_count >= job.max_attempts:
                # Crash at the final attempt (stale running, expired claim):
                # terminalize in this same claim transaction — the commit makes
                # it durable; raising here would roll it back and leak a
                # permanently running row.
                job.status = "failed"
                job.last_error_code = "company_knowledge_import_attempts_exhausted"
                job.last_error = None
                job.claim_token = None
                job.claim_expires_at = None
                return job
            job.status = "running"
            job.claim_token = claim_token
            job.claim_expires_at = now + timedelta(minutes=5)
            job.attempt_count += 1

        try:
            async with tenant_scoped_session(
                tenant_id,
                session_factory=session_factory,
                require_tenant=True,
                source="company_knowledge_import_process",
            ) as session:
                job = (
                    await session.execute(
                        select(CompanyKnowledgeImportJob)
                        .where(
                            CompanyKnowledgeImportJob.id == job_id,
                            CompanyKnowledgeImportJob.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one()
                if job.status == "completed":
                    return job
                if job.status != "running" or job.claim_token != claim_token:
                    raise RuntimeError("company_knowledge_import_claim_lost")
                request = dict(job.request_json)
                artifact = _ensure_relative_to(Path(str(job.artifact_ref)), self._data_root)
                if not artifact.exists():
                    raise CompanyKnowledgeImportError("source_missing")
                payload = artifact.read_bytes()
                if hashlib.sha256(payload).hexdigest() != job.artifact_hash:
                    raise ValueError("company_knowledge_import_artifact_hash_mismatch")
                if isinstance(request.get("direct_file_import"), dict) and not request.get("conversion_receipt"):
                    # Direct file import: the spooled source bytes convert in
                    # the worker (never in the request transaction). A stored
                    # conversion receipt marks the artifact as already-canonical
                    # so a retry never reconverts it.
                    payload, artifact = await self._convert_direct_file_payload(
                        session,
                        job=job,
                        request=request,
                        payload=payload,
                        tenant_id=tenant_id,
                    )
                contract = (
                    await session.execute(
                        select(CompanyKnowledgeSourceContract).where(
                            CompanyKnowledgeSourceContract.id == job.source_contract_id,
                            CompanyKnowledgeSourceContract.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one()
                if contract.version != job.source_contract_version:
                    raise ValueError("company_knowledge_source_contract_version_drift")

                principal_data = dict(request["principal"])
                principal = CompanyKnowledgePrincipal(
                    tenant_id=tenant_id,
                    accountable_user_id=job.accountable_user_id,
                    accountable_role=str(principal_data["accountable_role"]),
                    actor_type=job.created_by_type,
                    actor_id=job.created_by_id,
                    department_id=uuid.UUID(principal_data["department_id"])
                    if principal_data.get("department_id")
                    else None,
                    team_ids=tuple(uuid.UUID(value) for value in principal_data.get("team_ids", [])),
                    purpose=principal_data.get("purpose"),
                    session_id=principal_data.get("session_id"),
                    runtime_task_id=principal_data.get("runtime_task_id"),
                    workflow_run_id=principal_data.get("workflow_run_id"),
                    delegation_id=principal_data.get("delegation_id"),
                )
                source = (
                    await session.execute(
                        select(CompanyKnowledgeSource)
                        .where(
                            CompanyKnowledgeSource.tenant_id == tenant_id,
                            CompanyKnowledgeSource.source_contract_id == contract.id,
                            CompanyKnowledgeSource.source_item_id == request["source_item_id"],
                            CompanyKnowledgeSource.source_revision == request["source_revision"],
                        )
                        .limit(1)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if source is None:
                    source = CompanyKnowledgeSource(
                        tenant_id=tenant_id,
                        source_contract_id=contract.id,
                        source_contract_version=contract.version,
                        source_item_id=request["source_item_id"],
                        source_revision=request["source_revision"],
                        namespace=request["proposed_namespace"],
                        sensitivity=request["proposed_sensitivity"],
                        canonical_artifact_ref=str(artifact),
                        content_hash=job.artifact_hash,
                        source_acl_snapshot_hash=request["source_acl_snapshot_hash"],
                        source_acl_snapshot_json=dict(request["source_acl_snapshot"]),
                        retention_state_json=dict(contract.retention_policy_json or {}),
                        legal_hold=False,
                        cursor_json=dict(request.get("cursor") or {}),
                        lineage_json={
                            "source_contract_id": str(contract.id),
                            "source_contract_version": contract.version,
                            "import_job_id": str(job.id),
                        },
                        status="registered",
                        created_by_type=job.created_by_type,
                        created_by_id=job.created_by_id,
                    )
                    session.add(source)
                    await session.flush()
                    await append_company_knowledge_event(
                        session,
                        event_input=_event_input(
                            principal=principal,
                            event_type="company_knowledge.source_registered",
                            resource_type="source",
                            resource_id=source.id,
                            resource_version=contract.version,
                            source_refs=(f"company-source-contract://{contract.id}",),
                            source_hash=source.content_hash,
                            policy_snapshot=dict(request["permission_decision"]),
                            trace_id=job.trace_id,
                            idempotency_key=f"{job.idempotency_key}:source",
                            outcome="registered",
                            payload={"source_item_id": source.source_item_id, "status": source.status},
                        ),
                    )

                evidence_id = uuid.UUID(str(request["evidence_id"]))
                evidence = (
                    await session.execute(
                        select(CompanyKnowledgeEvidence)
                        .where(
                            CompanyKnowledgeEvidence.tenant_id == tenant_id,
                            CompanyKnowledgeEvidence.idempotency_key == job.idempotency_key,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                observed_at = _parse_datetime(request["observed_at"]) or _utcnow()
                if evidence is None:
                    typed_payload = request.get("typed_payload")
                    evidence_input = CanonicalEvidenceInput(
                        evidence_id=evidence_id,
                        tenant_id=tenant_id,
                        source_contract_id=contract.id,
                        source_contract_version=contract.version,
                        evidence_kind=request["evidence_kind"],
                        source_item_id=request["source_item_id"],
                        source_revision=request["source_revision"],
                        artifact_ref=str(artifact),
                        schema_ref=request.get("schema_ref"),
                        typed_payload_ref=str(artifact)
                        if request["evidence_kind"] in {"structured_record", "event"}
                        else None,
                        typed_payload=typed_payload,
                        content_hash=job.artifact_hash,
                        source_acl_snapshot_hash=request["source_acl_snapshot_hash"],
                        source_acl_snapshot=dict(request["source_acl_snapshot"]),
                        occurred_at=_parse_datetime(request.get("occurred_at")),
                        effective_from=_parse_datetime(request.get("effective_from")),
                        effective_until=_parse_datetime(request.get("effective_until")),
                        observed_at=observed_at,
                        cursor=dict(request.get("cursor") or {}),
                        sequence=request.get("sequence"),
                        idempotency_key=job.idempotency_key,
                        coverage_ledger_ref=f"company-import://{job.id}/coverage",
                        coverage_ledger=dict(request["coverage_ledger"]),
                        ingestion_receipt_ref=f"company-import://{job.id}",
                    )
                    envelope = build_canonical_evidence_envelope(evidence_input)
                    evidence = CompanyKnowledgeEvidence(
                        id=evidence_id,
                        tenant_id=tenant_id,
                        source_id=source.id,
                        source_contract_id=contract.id,
                        source_contract_version=contract.version,
                        evidence_kind=request["evidence_kind"],
                        source_item_id=request["source_item_id"],
                        source_revision=request["source_revision"],
                        artifact_ref=str(artifact),
                        schema_ref=request.get("schema_ref"),
                        typed_payload_ref=str(artifact)
                        if request["evidence_kind"] in {"structured_record", "event"}
                        else None,
                        canonical_envelope_json=envelope,
                        content_hash=job.artifact_hash,
                        source_acl_snapshot_hash=request["source_acl_snapshot_hash"],
                        source_acl_snapshot_json=dict(request["source_acl_snapshot"]),
                        occurred_at=_parse_datetime(request.get("occurred_at")),
                        effective_from=_parse_datetime(request.get("effective_from")),
                        effective_until=_parse_datetime(request.get("effective_until")),
                        observed_at=observed_at,
                        cursor_json=dict(request.get("cursor") or {}),
                        sequence=request.get("sequence"),
                        idempotency_key=job.idempotency_key,
                        coverage_ledger_ref=f"company-import://{job.id}/coverage",
                        coverage_ledger_json=dict(request["coverage_ledger"]),
                        ingestion_receipt_ref=f"company-import://{job.id}",
                        status="accepted",
                    )
                    session.add(evidence)
                    await session.flush()

                markdown = payload.decode("utf-8") if request["evidence_kind"] == "document" else ""
                document = (
                    await session.execute(
                        select(KnowledgeDocument)
                        .where(
                            KnowledgeDocument.tenant_id == tenant_id,
                            KnowledgeDocument.scope_type == "company",
                            KnowledgeDocument.scope_id == tenant_id,
                            KnowledgeDocument.source_sha256 == job.artifact_hash,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if document is None:
                    document = KnowledgeDocument(
                        tenant_id=tenant_id,
                        scope_type="company",
                        scope_id=tenant_id,
                        owner_user_id=None,
                        source_kind=request["evidence_kind"],
                        source_uri=str(artifact),
                        source_sha256=job.artifact_hash,
                        artifact_hash=job.artifact_hash,
                        title=clean_title(request["title"]),
                        status="ready",
                        sensitivity=request["proposed_sensitivity"],
                        agent_searchable=True,
                        canonical_md_path=str(artifact),
                        canonical_md_sha256=job.artifact_hash,
                        doc_metadata_json={
                            "company_knowledge": {
                                "namespace": request["proposed_namespace"],
                                "source_id": str(source.id),
                                "evidence_id": str(evidence.id),
                                "source_acl_snapshot_hash": request["source_acl_snapshot_hash"],
                                "source_acl_snapshot": request["source_acl_snapshot"],
                                "coverage_complete": True,
                            }
                        },
                        created_by_user_id=job.accountable_user_id,
                    )
                    session.add(document)
                    await session.flush()
                    for draft in segment_markdown(markdown):
                        session.add(
                            KnowledgeSegment(
                                tenant_id=tenant_id,
                                document_id=document.id,
                                scope_type="company",
                                scope_id=tenant_id,
                                position=draft.position,
                                segment_hash=draft.segment_hash,
                                heading_path_json=draft.heading_path,
                                content=draft.content,
                                token_count=draft.token_count,
                                tsv=None,
                                segment_metadata_json={
                                    "namespace": request["proposed_namespace"],
                                    "evidence_id": str(evidence.id),
                                },
                            )
                        )
                    await session.flush()

                await self._complete_promotion_handoff(
                    session,
                    principal=principal,
                    job=job,
                    request=request,
                    source=source,
                    evidence=evidence,
                    document=document,
                    markdown=markdown,
                )
                source.status = "ingested"
                job.source_id = source.id
                job.evidence_id = evidence.id
                job.document_id = document.id
                job.status = "completed"
                job.completed_at = _utcnow()
                job.claim_token = None
                job.claim_expires_at = None
                job.last_error = None
                job.last_error_code = None
                await append_company_knowledge_event_with_outbox(
                    session,
                    event_input=_event_input(
                        principal=principal,
                        event_type="company_knowledge.ingest_completed",
                        resource_type="evidence",
                        resource_id=evidence.id,
                        resource_version=contract.version,
                        source_refs=(f"company-source://{source.id}", f"company-evidence://{evidence.id}"),
                        source_hash=evidence.content_hash,
                        policy_snapshot=dict(request["permission_decision"]),
                        trace_id=job.trace_id,
                        idempotency_key=f"{job.idempotency_key}:completed",
                        outcome="completed",
                        payload={
                            "job_id": str(job.id),
                            "document_id": str(document.id),
                            "evidence_id": str(evidence.id),
                        },
                    ),
                    aggregate_type="knowledge_document",
                    aggregate_id=document.id,
                    outbox_event_type="company_knowledge.document_index_requested",
                    outbox_idempotency_key=f"{job.idempotency_key}:index",
                    outbox_payload={
                        "operation": "index_document",
                        "document_id": str(document.id),
                        "source_evidence_id": str(evidence.id),
                    },
                    available_at=_utcnow(),
                )
                return job
        except Exception as exc:
            async with tenant_scoped_session(
                tenant_id,
                session_factory=session_factory,
                require_tenant=True,
                source="company_knowledge_import_failure",
            ) as session:
                failed = (
                    await session.execute(
                        select(CompanyKnowledgeImportJob)
                        .where(
                            CompanyKnowledgeImportJob.id == job_id,
                            CompanyKnowledgeImportJob.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if failed is not None and failed.status != "completed":
                    failed.status = "failed" if failed.attempt_count >= failed.max_attempts else "queued"
                    failed.available_at = _utcnow() + timedelta(seconds=min(300, 2**failed.attempt_count))
                    failed.claim_token = None
                    failed.claim_expires_at = None
                    failed.last_error_code = str(getattr(exc, "code", "") or "") or type(exc).__name__
                    failed.last_error = str(exc)[:4000]
            raise

    async def recover_due_import_jobs(
        self,
        session: Any,
        *,
        session_factory: Any,
        limit: int = 50,
    ) -> CompanyKnowledgeImportRecoverySummary:
        """Recover queued/retryable/stale-running jobs discovered under audited bypass."""

        if limit < 1:
            raise ValueError("company_knowledge_recovery_limit_must_be_positive")
        now = _utcnow()
        rows = (
            await session.execute(
                select(
                    CompanyKnowledgeImportJob.tenant_id,
                    CompanyKnowledgeImportJob.id,
                )
                .where(
                    or_(
                        and_(
                            CompanyKnowledgeImportJob.status.in_(("queued", "failed")),
                            CompanyKnowledgeImportJob.available_at <= now,
                            CompanyKnowledgeImportJob.attempt_count < CompanyKnowledgeImportJob.max_attempts,
                        ),
                        # Stale running is recovered at any attempt count: a
                        # crash at the final attempt must be terminalized, not
                        # filtered out and left permanently running.
                        and_(
                            CompanyKnowledgeImportJob.status == "running",
                            CompanyKnowledgeImportJob.claim_expires_at.is_not(None),
                            CompanyKnowledgeImportJob.claim_expires_at <= now,
                        ),
                    ),
                )
                .order_by(
                    CompanyKnowledgeImportJob.available_at,
                    CompanyKnowledgeImportJob.created_at,
                    CompanyKnowledgeImportJob.id,
                )
                .limit(limit)
            )
        ).all()
        refs = tuple((uuid.UUID(str(row[0])), uuid.UUID(str(row[1]))) for row in rows)
        completed = 0
        failed = 0
        skipped = 0
        for tenant_id, job_id in refs:
            try:
                job = await self.process_import_job(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    session_factory=session_factory,
                )
                if job.status == "completed":
                    completed += 1
                elif job.status == "failed":
                    failed += 1
                else:
                    skipped += 1
            except RuntimeError as exc:
                if str(exc) in {
                    "company_knowledge_import_job_already_claimed",
                    "company_knowledge_import_claim_lost",
                }:
                    skipped += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        return CompanyKnowledgeImportRecoverySummary(
            attempted=len(refs),
            completed=completed,
            failed=failed,
            skipped=skipped,
            job_refs=refs,
        )

    async def list_import_jobs(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        limit: int = 50,
    ) -> list[CompanyKnowledgeImportJobSummary]:
        rows = (
            (
                await session.execute(
                    select(CompanyKnowledgeImportJob)
                    .where(CompanyKnowledgeImportJob.tenant_id == tenant_id)
                    .order_by(CompanyKnowledgeImportJob.created_at.desc())
                    .limit(max(1, int(limit or 50)))
                )
            )
            .scalars()
            .all()
        )
        return [company_import_job_view(job) for job in rows]

    async def get_import_job_summary(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> CompanyKnowledgeImportJobSummary | None:
        job = (
            await session.execute(
                select(CompanyKnowledgeImportJob).where(
                    CompanyKnowledgeImportJob.id == job_id,
                    CompanyKnowledgeImportJob.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return company_import_job_view(job) if job is not None else None

    async def retry_import_job(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> CompanyKnowledgeImportJobSummary:
        """Requeue a failed/cancelled job without running any work.

        The retry only commits the queued transition and clears the stale
        terminal fields; the caller schedules the asynchronous worker. The
        attempt ceiling and permanent failure codes reject with typed
        conflicts instead of a futile requeue.
        """
        job = (
            await session.execute(
                select(CompanyKnowledgeImportJob)
                .where(
                    CompanyKnowledgeImportJob.id == job_id,
                    CompanyKnowledgeImportJob.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            raise LookupError("company_knowledge_import_job_not_found")
        status = str(job.status or "")
        if status not in {"failed", "cancelled"}:
            raise CompanyKnowledgeJobConflict("not_retryable")
        if int(job.attempt_count or 0) >= int(job.max_attempts or 0):
            raise CompanyKnowledgeJobConflict("retry_attempt_limit")
        if str(job.last_error_code or "") in _PERMANENT_IMPORT_ERROR_CODES:
            raise CompanyKnowledgeJobConflict("not_retryable")
        job.status = "queued"
        job.available_at = _utcnow()
        job.claim_token = None
        job.claim_expires_at = None
        job.last_error = None
        job.last_error_code = None
        request = dict(job.request_json or {})
        request.pop("cancelled_at", None)
        request["retried_at"] = _utcnow().isoformat()
        job.request_json = request
        await session.flush()
        refresh = getattr(session, "refresh", None)
        if refresh is not None:
            await refresh(job)
        return company_import_job_view(job)

    async def cancel_import_job(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> CompanyKnowledgeImportJobSummary:
        """Cancel a queued job; running/terminal states reject with typed conflicts."""
        job = (
            await session.execute(
                select(CompanyKnowledgeImportJob)
                .where(
                    CompanyKnowledgeImportJob.id == job_id,
                    CompanyKnowledgeImportJob.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            raise LookupError("company_knowledge_import_job_not_found")
        status = str(job.status or "")
        if status != "queued":
            code = "not_cancellable_while_running" if status == "running" else "not_cancellable_terminal"
            raise CompanyKnowledgeJobConflict(code)
        job.status = "cancelled"
        job.claim_token = None
        job.claim_expires_at = None
        cancelled_at = _utcnow().isoformat()
        request = dict(job.request_json or {})
        request["cancelled_at"] = cancelled_at
        job.request_json = request
        await session.flush()
        refresh = getattr(session, "refresh", None)
        if refresh is not None:
            await refresh(job)
        return company_import_job_view(job)

    async def get_import_job_preview(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> CompanyKnowledgeImportPreview | None:
        """Pre-publication segment preview for a completed import job."""
        job = (
            await session.execute(
                select(CompanyKnowledgeImportJob).where(
                    CompanyKnowledgeImportJob.id == job_id,
                    CompanyKnowledgeImportJob.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        if str(job.status or "") != "completed" or job.document_id is None:
            raise CompanyKnowledgeJobConflict("preview_requires_completed")
        document = (
            await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.id == job.document_id,
                    KnowledgeDocument.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if document is None:
            raise CompanyKnowledgeJobConflict("preview_requires_completed")
        request = dict(job.request_json or {})
        segments = (
            (
                await session.execute(
                    select(KnowledgeSegment)
                    .where(KnowledgeSegment.document_id == document.id)
                    .order_by(KnowledgeSegment.position.asc())
                )
            )
            .scalars()
            .all()
        )
        return CompanyKnowledgeImportPreview(
            job_id=job.id,
            document_id=document.id,
            evidence_id=job.evidence_id,
            source_id=job.source_id,
            proposal_id=job.proposal_id,
            title=str(document.title or ""),
            namespace=str(request.get("proposed_namespace") or ""),
            sensitivity=str(document.sensitivity or ""),
            segments=[
                CompanyKnowledgeImportPreviewSegment(
                    segment_id=segment.id,
                    position=int(segment.position or 0),
                    heading_path=list(segment.heading_path_json or []),
                    content=str(segment.content or ""),
                    token_count=int(segment.token_count or 0),
                )
                for segment in segments
            ],
        )

    async def create_proposal_from_import(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        job_id: uuid.UUID,
        trace_id: str,
    ) -> CompanyKnowledgeProposal:
        """Create + submit the review proposal for a completed import.

        Idempotent: the proposal binds the job's exact evidence/document and a
        deterministic idempotency key; a repeated call returns the existing
        proposal. The document produced by the import is the canonical
        candidate — no second copy is materialized.
        """
        job = (
            await session.execute(
                select(CompanyKnowledgeImportJob)
                .where(
                    CompanyKnowledgeImportJob.id == job_id,
                    CompanyKnowledgeImportJob.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            raise LookupError("company_knowledge_import_job_not_found")
        if job.proposal_id is not None:
            existing = (
                await session.execute(
                    select(CompanyKnowledgeProposal).where(
                        CompanyKnowledgeProposal.id == job.proposal_id,
                        CompanyKnowledgeProposal.tenant_id == principal.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        if (
            str(job.status or "") != "completed"
            or job.document_id is None
            or job.source_id is None
            or job.evidence_id is None
        ):
            raise CompanyKnowledgeJobConflict("proposal_requires_completed_import")
        document = (
            await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.id == job.document_id,
                    KnowledgeDocument.tenant_id == principal.tenant_id,
                )
            )
        ).scalar_one()
        request = dict(job.request_json or {})
        evidence_ref = f"company-evidence://{job.evidence_id}"
        proposal = await self.create_proposal(
            session,
            principal=principal,
            request=CompanyKnowledgeProposalRequest(
                proposal_kind="knowledge",
                source_id=job.source_id,
                source_document_id=job.document_id,
                source_revision_ref=f"company-import://{job.id}",
                baseline_publication_id=None,
                baseline_version=None,
                proposed_patch={
                    "operation": "direct_import",
                    "origin": "direct_import",
                    "title": str(document.title or ""),
                    "content_hash": str(document.canonical_md_sha256 or ""),
                    "content_ref": f"company-import://{job.id}/candidate",
                    "reason": str(request.get("purpose") or ""),
                },
                proposed_namespace=str(request.get("proposed_namespace") or ""),
                proposed_sensitivity=str(request.get("proposed_sensitivity") or "internal"),
                source_refs=(evidence_ref,),
                source_coverage=dict(
                    request.get("coverage_ledger") or {"complete": True, "covered_units": 1, "total_units": 1}
                ),
                conflict_candidates=(),
                ontology_mapping={},
                risk_level="normal",
                required_review_policy=default_company_knowledge_review_policy(
                    proposed_sensitivity=str(request.get("proposed_sensitivity") or "internal"),
                    risk_level="normal",
                    created_by_type=principal.actor_type,
                ),
                idempotency_key=f"{job.idempotency_key}:direct-import-proposal",
                trace_id=trace_id,
            ),
        )
        if proposal.status == "draft":
            proposal = await self.submit_proposal(
                session,
                principal=principal,
                proposal_id=proposal.id,
                expected_state_version=proposal.state_version,
                trace_id=trace_id,
            )
        elif proposal.status != "submitted":
            raise RuntimeError("company_knowledge_direct_import_proposal_state_conflict")
        job.proposal_id = proposal.id
        await session.flush()
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.direct_import_proposal_submitted",
                resource_type="import_job",
                resource_id=job.id,
                resource_version=job.attempt_count,
                source_refs=(evidence_ref,),
                source_hash=str(document.canonical_md_sha256 or ""),
                policy_snapshot=dict(request.get("permission_decision") or {}),
                trace_id=trace_id,
                idempotency_key=f"{job.idempotency_key}:direct-import-proposal-submitted",
                outcome="submitted",
                payload={"proposal_id": str(proposal.id), "origin": "direct_import"},
            ),
        )
        return proposal

    async def create_proposal(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: CompanyKnowledgeProposalRequest,
    ) -> CompanyKnowledgeProposal:
        if request.proposal_kind not in _PROPOSAL_KINDS:
            raise ValueError("unsupported_company_knowledge_proposal_kind")
        if request.risk_level not in _RISK_LEVELS:
            raise ValueError("unsupported_company_knowledge_risk_level")
        if request.source_coverage.get("complete") is not True or request.source_coverage.get(
            "covered_units"
        ) != request.source_coverage.get("total_units"):
            raise ValueError("complete_company_knowledge_source_coverage_required")
        sensitivity = canonicalize_sensitivity(request.proposed_sensitivity).value
        source = None
        if request.source_id:
            source = (
                await session.execute(
                    select(CompanyKnowledgeSource).where(
                        CompanyKnowledgeSource.id == request.source_id,
                        CompanyKnowledgeSource.tenant_id == principal.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if source is None:
                raise LookupError("company_knowledge_source_not_found")
        if request.source_document_id:
            document = (
                await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.id == request.source_document_id,
                        KnowledgeDocument.tenant_id == principal.tenant_id,
                        KnowledgeDocument.scope_type == "company",
                        KnowledgeDocument.scope_id == principal.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if document is None:
                raise LookupError("company_knowledge_document_not_found")
        resource = CompanyKnowledgeResource(
            tenant_id=principal.tenant_id,
            resource_type="company_knowledge_namespace",
            resource_id=None,
            resource_key=f"namespace:{request.proposed_namespace}",
            namespace=request.proposed_namespace,
            sensitivity=sensitivity,
            source_acl_snapshot_hash=source.source_acl_snapshot_hash if source else None,
            source_acl=dict(source.source_acl_snapshot_json) if source else None,
            evidence_access_complete=True,
            publication_status=None,
        )
        policy = await self._require_permission(
            session,
            principal=principal,
            resource=resource,
            action="propose",
        )
        proposed_hash = _hash_json(request.proposed_patch)
        existing = (
            await session.execute(
                select(CompanyKnowledgeProposal)
                .where(
                    CompanyKnowledgeProposal.tenant_id == principal.tenant_id,
                    CompanyKnowledgeProposal.idempotency_key == request.idempotency_key,
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.proposed_content_hash != proposed_hash:
                raise ValueError("company_knowledge_proposal_idempotency_conflict")
            return existing
        proposal = CompanyKnowledgeProposal(
            tenant_id=principal.tenant_id,
            idempotency_key=request.idempotency_key,
            proposal_kind=request.proposal_kind,
            source_id=request.source_id,
            source_document_id=request.source_document_id,
            source_revision_ref=request.source_revision_ref,
            baseline_publication_id=request.baseline_publication_id,
            baseline_version=request.baseline_version,
            proposed_patch_json=dict(request.proposed_patch),
            proposed_content_hash=proposed_hash,
            proposed_namespace=request.proposed_namespace,
            proposed_sensitivity=sensitivity,
            source_refs_json=list(request.source_refs),
            source_coverage_json=dict(request.source_coverage),
            conflict_candidates_json=list(request.conflict_candidates),
            ontology_mapping_json=dict(request.ontology_mapping),
            status="draft",
            risk_level=request.risk_level,
            required_review_policy_json=dict(request.required_review_policy),
            created_by_type=principal.actor_type,
            created_by_id=principal.actor_id,
            accountable_user_id=principal.accountable_user_id,
            state_version=1,
        )
        session.add(proposal)
        await session.flush()
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.proposal_created",
                resource_type="proposal",
                resource_id=proposal.id,
                resource_version=proposal.state_version,
                source_refs=request.source_refs,
                source_hash=proposal.proposed_content_hash,
                policy_snapshot=policy,
                trace_id=request.trace_id,
                idempotency_key=f"{request.idempotency_key}:created",
                outcome="draft",
                payload={"status": "draft", "proposal_kind": proposal.proposal_kind},
            ),
        )
        return proposal

    @staticmethod
    async def _locked_proposal(
        session: Any,
        *,
        tenant_id: uuid.UUID,
        proposal_id: uuid.UUID,
        expected_state_version: int,
    ) -> CompanyKnowledgeProposal:
        proposal = (
            await session.execute(
                select(CompanyKnowledgeProposal)
                .where(
                    CompanyKnowledgeProposal.id == proposal_id,
                    CompanyKnowledgeProposal.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if proposal is None:
            raise LookupError("company_knowledge_proposal_not_found")
        if proposal.state_version != expected_state_version:
            raise RuntimeError("company_knowledge_proposal_state_conflict")
        return proposal

    async def submit_proposal(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        proposal_id: uuid.UUID,
        expected_state_version: int,
        trace_id: str,
    ) -> CompanyKnowledgeProposal:
        proposal = await self._locked_proposal(
            session,
            tenant_id=principal.tenant_id,
            proposal_id=proposal_id,
            expected_state_version=expected_state_version,
        )
        resource = CompanyKnowledgeResource(
            tenant_id=principal.tenant_id,
            resource_type="company_knowledge_proposal",
            resource_id=proposal.id,
            resource_key=f"proposal:{proposal.id}",
            namespace=proposal.proposed_namespace,
            sensitivity=proposal.proposed_sensitivity,
            source_acl_snapshot_hash=None,
            source_acl=None,
            evidence_access_complete=bool(proposal.source_coverage_json.get("complete")),
            publication_status=None,
        )
        policy = await self._require_permission(
            session,
            principal=principal,
            resource=resource,
            action="propose",
        )
        proposal.status = next_company_proposal_status(proposal.status, "submit")
        proposal.state_version += 1
        proposal.submitted_at = _utcnow()
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.proposal_submitted",
                resource_type="proposal",
                resource_id=proposal.id,
                resource_version=proposal.state_version,
                source_refs=tuple(proposal.source_refs_json or []),
                source_hash=proposal.proposed_content_hash,
                policy_snapshot=policy,
                trace_id=trace_id,
                idempotency_key=f"{proposal.idempotency_key}:submitted:{proposal.state_version}",
                outcome=proposal.status,
                payload={"status": proposal.status},
            ),
        )
        return proposal

    @staticmethod
    def _requires_materialization(proposal: CompanyKnowledgeProposal) -> bool:
        return company_knowledge_proposal_requires_materialization(
            proposal_kind=proposal.proposal_kind,
            proposed_patch=dict(proposal.proposed_patch_json or {}),
        )

    @staticmethod
    def _review_subject_hash(proposal: CompanyKnowledgeProposal) -> str:
        if CompanyKnowledgeService._requires_materialization(proposal):
            if not proposal.materialization_content_hash or proposal.materialized_document_id is None:
                raise ValueError("company_knowledge_materialization_required_before_review")
            CompanyKnowledgeService._validated_materialization_receipt(proposal)
            return str(proposal.materialization_content_hash)
        return str(proposal.proposed_content_hash)

    @staticmethod
    def _validated_materialization_receipt(
        proposal: CompanyKnowledgeProposal,
    ) -> dict[str, Any]:
        receipt = dict(proposal.materialization_receipt_json or {})
        expected = {
            "schema": "hive.company_knowledge_materialization.v1",
            "candidate_hash": proposal.proposed_content_hash,
            "content_hash": proposal.materialization_content_hash,
            "document_id": (
                str(proposal.materialized_document_id) if proposal.materialized_document_id is not None else None
            ),
            "source_refs_hash": _hash_json(list(proposal.source_refs_json or [])),
            "attested_by_user_id": (
                str(proposal.materialized_by_user_id) if proposal.materialized_by_user_id is not None else None
            ),
            "attestation": "candidate_applied",
        }
        if (
            any(receipt.get(key) != value for key, value in expected.items())
            or proposal.materialized_at is None
            or len(str(receipt.get("request_hash") or "")) != 64
            or len(str(receipt.get("title_hash") or "")) != 64
        ):
            raise RuntimeError("company_knowledge_materialization_receipt_drift")
        return receipt

    @staticmethod
    def _company_evidence_id_from_ref(source_ref: str) -> uuid.UUID:
        prefix = "company-evidence://"
        rendered = str(source_ref).strip()
        if not rendered.startswith(prefix):
            raise PermissionError("company_knowledge_proposal_evidence_ref_invalid")
        try:
            return uuid.UUID(rendered.removeprefix(prefix).split("#", 1)[0])
        except ValueError as exc:
            raise PermissionError("company_knowledge_proposal_evidence_ref_invalid") from exc

    async def _proposal_permission_resources(
        self,
        session: Any,
        *,
        proposal: CompanyKnowledgeProposal,
    ) -> tuple[CompanyKnowledgeResource, ...]:
        source = None
        if proposal.source_id is not None:
            source = (
                await session.execute(
                    select(CompanyKnowledgeSource).where(
                        CompanyKnowledgeSource.id == proposal.source_id,
                        CompanyKnowledgeSource.tenant_id == proposal.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if source is None:
                raise LookupError("company_knowledge_source_not_found")
            return (
                CompanyKnowledgeResource(
                    tenant_id=proposal.tenant_id,
                    resource_type="company_knowledge_proposal",
                    resource_id=proposal.id,
                    resource_key=f"proposal:{proposal.id}",
                    namespace=proposal.proposed_namespace,
                    sensitivity=proposal.proposed_sensitivity,
                    source_acl_snapshot_hash=source.source_acl_snapshot_hash,
                    source_acl=dict(source.source_acl_snapshot_json),
                    evidence_access_complete=bool(proposal.source_coverage_json.get("complete")),
                    publication_status=None,
                ),
            )

        evidence_ids = tuple(
            sorted(
                {self._company_evidence_id_from_ref(source_ref) for source_ref in proposal.source_refs_json or []},
                key=str,
            )
        )
        if not evidence_ids:
            raise PermissionError("company_knowledge_proposal_source_authority_unavailable")
        evidence_rows = (
            (
                await session.execute(
                    select(CompanyKnowledgeEvidence).where(
                        CompanyKnowledgeEvidence.tenant_id == proposal.tenant_id,
                        CompanyKnowledgeEvidence.id.in_(evidence_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        evidence_by_id = {row.id: row for row in evidence_rows}
        if set(evidence_by_id) != set(evidence_ids) or any(row.status != "accepted" for row in evidence_rows):
            raise PermissionError("company_knowledge_proposal_evidence_unavailable")
        proposal_coverage_complete = bool(proposal.source_coverage_json.get("complete"))
        return tuple(
            CompanyKnowledgeResource(
                tenant_id=proposal.tenant_id,
                resource_type="company_knowledge_proposal",
                resource_id=proposal.id,
                resource_key=f"proposal:{proposal.id}",
                namespace=proposal.proposed_namespace,
                sensitivity=proposal.proposed_sensitivity,
                source_acl_snapshot_hash=evidence_by_id[evidence_id].source_acl_snapshot_hash,
                source_acl=dict(evidence_by_id[evidence_id].source_acl_snapshot_json),
                evidence_access_complete=(
                    proposal_coverage_complete
                    and bool(evidence_by_id[evidence_id].coverage_ledger_json.get("complete"))
                ),
                publication_status=None,
            )
            for evidence_id in evidence_ids
        )

    async def authorize_proposal_action(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        proposal: CompanyKnowledgeProposal,
        action: str,
    ) -> dict[str, Any]:
        """Authorize one proposal action against every bound source authority."""

        decisions = [
            await self._require_permission(
                session,
                principal=principal,
                resource=resource,
                action=action,
            )
            for resource in await self._proposal_permission_resources(
                session,
                proposal=proposal,
            )
        ]
        if len(decisions) == 1:
            return decisions[0]
        return {
            "schema": "hive.company_knowledge_composite_permission_decision.v1",
            "allowed": True,
            "requested_action": action,
            "authority_sources": sorted(
                {source for decision in decisions for source in decision.get("authority_sources", [])}
            ),
            "source_acl_snapshot_hashes": sorted(
                {
                    str(snapshot_hash)
                    for decision in decisions
                    if (snapshot_hash := decision.get("source_acl_snapshot_hash"))
                }
            ),
            "decisions": decisions,
        }

    async def get_proposal_for_review(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        proposal_id: uuid.UUID,
    ) -> CompanyKnowledgeProposal:
        proposal = (
            await session.execute(
                select(CompanyKnowledgeProposal).where(
                    CompanyKnowledgeProposal.id == proposal_id,
                    CompanyKnowledgeProposal.tenant_id == principal.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if proposal is None:
            raise LookupError("company_knowledge_proposal_not_found")
        if proposal.status in {"draft", "withdrawn"}:
            raise LookupError("company_knowledge_proposal_not_found")
        await self.authorize_proposal_action(
            session,
            principal=principal,
            proposal=proposal,
            action="review",
        )
        return proposal

    async def materialize_proposal(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        proposal_id: uuid.UUID,
        request: CompanyKnowledgeMaterializationRequest,
        expected_state_version: int,
        trace_id: str,
    ) -> CompanyKnowledgeProposal:
        if principal.actor_type != "user":
            raise PermissionError("agents_cannot_materialize_company_knowledge")
        if request.attest_candidate_applied is not True:
            raise ValueError("company_knowledge_materialization_attestation_required")
        if not request.idempotency_key.strip():
            raise ValueError("company_knowledge_materialization_idempotency_required")
        normalized = normalize_markdown(request.markdown)
        if not normalized:
            raise ValueError("company_knowledge_materialization_markdown_required")
        title = clean_title(request.title)
        payload = normalized.encode("utf-8")
        content_hash = hashlib.sha256(payload).hexdigest()
        request_hash = _hash_json(
            {
                "proposal_id": proposal_id,
                "expected_proposed_content_hash": request.expected_proposed_content_hash,
                "title": title,
                "content_hash": content_hash,
                "attest_candidate_applied": request.attest_candidate_applied,
            }
        )
        proposal = (
            await session.execute(
                select(CompanyKnowledgeProposal)
                .where(
                    CompanyKnowledgeProposal.id == proposal_id,
                    CompanyKnowledgeProposal.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if proposal is None:
            raise LookupError("company_knowledge_proposal_not_found")
        if (
            proposal.materialization_idempotency_key == request.idempotency_key
            and dict(proposal.materialization_receipt_json or {}).get("request_hash") == request_hash
        ):
            return proposal
        if proposal.materialization_idempotency_key == request.idempotency_key:
            raise ValueError("company_knowledge_materialization_idempotency_conflict")
        for lock_key in (
            f"company-knowledge-materialization-key:{principal.tenant_id}:{request.idempotency_key}",
            f"company-knowledge-materialization-content:{principal.tenant_id}:{content_hash}",
        ):
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
        duplicate = (
            await session.execute(
                select(CompanyKnowledgeProposal.id)
                .where(
                    CompanyKnowledgeProposal.tenant_id == principal.tenant_id,
                    CompanyKnowledgeProposal.materialization_idempotency_key == request.idempotency_key,
                    CompanyKnowledgeProposal.id != proposal.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise ValueError("company_knowledge_materialization_idempotency_conflict")
        if proposal.state_version != expected_state_version:
            raise RuntimeError("company_knowledge_proposal_state_conflict")
        if proposal.status not in {"submitted", "in_review", "changes_requested", "approved"}:
            raise ValueError("company_knowledge_proposal_not_materializable")
        if not self._requires_materialization(proposal):
            raise ValueError("company_knowledge_proposal_does_not_require_materialization")
        if request.expected_proposed_content_hash != proposal.proposed_content_hash:
            raise RuntimeError("company_knowledge_materialization_candidate_drift")

        policy = await self.authorize_proposal_action(
            session,
            principal=principal,
            proposal=proposal,
            action="review",
        )
        baseline = None
        if proposal.source_document_id is not None:
            baseline = (
                await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.id == proposal.source_document_id,
                        KnowledgeDocument.tenant_id == principal.tenant_id,
                        KnowledgeDocument.scope_type == "company",
                        KnowledgeDocument.scope_id == principal.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if baseline is None:
                raise LookupError("company_knowledge_document_not_found")
        operation = str(dict(proposal.proposed_patch_json or {}).get("operation") or "")
        if operation == "agent_proposed_update" and baseline is not None and baseline.source_sha256 == content_hash:
            raise ValueError("agent_proposed_update_did_not_change_document")

        existing_document = (
            await session.execute(
                select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.tenant_id == principal.tenant_id,
                    KnowledgeDocument.scope_type == "company",
                    KnowledgeDocument.scope_id == principal.tenant_id,
                    KnowledgeDocument.source_sha256 == content_hash,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing_document is not None and existing_document.title != title:
            raise ValueError("company_knowledge_materialization_title_conflict")
        artifact_path = company_knowledge_artifact_path(
            self._data_root,
            tenant_id=principal.tenant_id,
            evidence_id=proposal.id,
            content_hash=content_hash,
            suffix=".md",
        )
        if existing_document is None:
            _atomic_write(artifact_path, payload)
            source = None
            if proposal.source_id is not None:
                source = await session.get(CompanyKnowledgeSource, proposal.source_id)
            existing_document = KnowledgeDocument(
                tenant_id=principal.tenant_id,
                scope_type="company",
                scope_id=principal.tenant_id,
                owner_user_id=None,
                source_kind="reviewer_materialization",
                source_uri=f"company-proposal://{proposal.id}",
                source_sha256=content_hash,
                artifact_hash=content_hash,
                title=title,
                status="ready",
                sensitivity=proposal.proposed_sensitivity,
                agent_searchable=True,
                canonical_md_path=str(artifact_path),
                canonical_md_sha256=content_hash,
                doc_metadata_json={
                    "company_knowledge": {
                        "namespace": proposal.proposed_namespace,
                        "source_id": str(proposal.source_id) if proposal.source_id else None,
                        "source_acl_snapshot_hash": (source.source_acl_snapshot_hash if source is not None else None),
                        "source_acl_snapshot": (dict(source.source_acl_snapshot_json) if source is not None else None),
                        "coverage_complete": bool(proposal.source_coverage_json.get("complete")),
                        "materialized_from_proposal_id": str(proposal.id),
                        "materialized_from_candidate_hash": proposal.proposed_content_hash,
                    }
                },
                created_by_user_id=principal.accountable_user_id,
            )
            session.add(existing_document)
            await session.flush()
            for draft in segment_markdown(normalized):
                session.add(
                    KnowledgeSegment(
                        tenant_id=principal.tenant_id,
                        document_id=existing_document.id,
                        scope_type="company",
                        scope_id=principal.tenant_id,
                        position=draft.position,
                        segment_hash=draft.segment_hash,
                        heading_path_json=draft.heading_path,
                        content=draft.content,
                        token_count=draft.token_count,
                        tsv=None,
                        segment_metadata_json={
                            "namespace": proposal.proposed_namespace,
                            "materialized_from_proposal_id": str(proposal.id),
                        },
                    )
                )
            await session.flush()

        previous_content_hash = proposal.materialization_content_hash
        now = _utcnow()
        proposal.materialized_document_id = existing_document.id
        proposal.materialization_content_hash = content_hash
        proposal.materialization_idempotency_key = request.idempotency_key
        proposal.materialized_by_user_id = principal.accountable_user_id
        proposal.materialized_at = now
        proposal.materialization_receipt_json = {
            "schema": "hive.company_knowledge_materialization.v1",
            "request_hash": request_hash,
            "candidate_hash": proposal.proposed_content_hash,
            "content_hash": content_hash,
            "title_hash": hashlib.sha256(title.encode("utf-8")).hexdigest(),
            "document_id": str(existing_document.id),
            "source_refs_hash": _hash_json(list(proposal.source_refs_json or [])),
            "previous_content_hash": previous_content_hash,
            "attested_by_user_id": str(principal.accountable_user_id),
            "attestation": "candidate_applied",
        }
        proposal.status = "submitted"
        proposal.submitted_at = now
        proposal.state_version += 1
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.proposal_materialized",
                resource_type="proposal",
                resource_id=proposal.id,
                resource_version=proposal.state_version,
                source_refs=tuple(proposal.source_refs_json or []),
                source_hash=content_hash,
                policy_snapshot=policy,
                trace_id=trace_id,
                idempotency_key=f"{request.idempotency_key}:materialized",
                outcome="submitted",
                payload={
                    "document_id": str(existing_document.id),
                    "candidate_hash": proposal.proposed_content_hash,
                    "content_hash": content_hash,
                    "requires_fresh_review": True,
                },
                occurred_at=now,
            ),
        )
        return proposal

    async def record_review(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        proposal_id: uuid.UUID,
        request: CompanyKnowledgeReviewRequest,
        expected_state_version: int,
        trace_id: str,
    ) -> CompanyKnowledgeProposal:
        if request.decision not in _REVIEW_DECISIONS:
            raise ValueError("unsupported_company_knowledge_review_decision")
        if principal.actor_type != "user" or request.reviewer_role == "agent":
            raise PermissionError("agents_cannot_review_company_knowledge")
        if request.reviewer_role != principal.accountable_role:
            raise PermissionError("company_knowledge_reviewer_role_mismatch")
        if not request.reason.strip() or not request.evidence_refs:
            raise ValueError("review_reason_and_evidence_required")
        proposal = await self._locked_proposal(
            session,
            tenant_id=principal.tenant_id,
            proposal_id=proposal_id,
            expected_state_version=expected_state_version,
        )
        expected_evidence_refs = tuple(sorted({str(value) for value in proposal.source_refs_json or []}))
        provided_evidence_refs = tuple(sorted({str(value) for value in request.evidence_refs}))
        if provided_evidence_refs != expected_evidence_refs:
            raise ValueError("company_knowledge_review_evidence_binding_mismatch")
        action = "approve" if request.decision == "approve" else "review"
        policy = await self.authorize_proposal_action(
            session,
            principal=principal,
            proposal=proposal,
            action=action,
        )
        if proposal.status == "submitted":
            proposal.status = next_company_proposal_status(proposal.status, "begin_review")
        if proposal.status != "in_review":
            raise ValueError("company_knowledge_proposal_not_in_review")
        subject_content_hash = self._review_subject_hash(proposal)
        review_round = (
            await session.scalar(
                select(func.coalesce(func.max(CompanyKnowledgeReview.review_round), 0)).where(
                    CompanyKnowledgeReview.tenant_id == principal.tenant_id,
                    CompanyKnowledgeReview.proposal_id == proposal.id,
                    CompanyKnowledgeReview.reviewer_user_id == principal.accountable_user_id,
                )
            )
        ) + 1
        decision_hash = _hash_json(
            {
                "proposal_id": proposal.id,
                "reviewer_user_id": principal.accountable_user_id,
                "reviewer_role": request.reviewer_role,
                "review_round": review_round,
                "decision": request.decision,
                "reason": request.reason,
                "evidence_refs": request.evidence_refs,
                "policy_snapshot": policy,
                "subject_content_hash": subject_content_hash,
            }
        )
        review = CompanyKnowledgeReview(
            tenant_id=principal.tenant_id,
            proposal_id=proposal.id,
            reviewer_user_id=principal.accountable_user_id,
            reviewer_role=request.reviewer_role,
            review_round=review_round,
            subject_content_hash=subject_content_hash,
            decision=request.decision,
            reason=request.reason,
            evidence_refs_json=list(request.evidence_refs),
            policy_snapshot_json={
                "schema": "hive.company_knowledge_review_authority.v1",
                "permission_decision": policy,
                "required_review_policy": dict(proposal.required_review_policy_json or {}),
                "subject_content_hash": subject_content_hash,
            },
            decision_hash=decision_hash,
        )
        session.add(review)
        await session.flush()
        rows = (
            (
                await session.execute(
                    select(CompanyKnowledgeReview)
                    .where(
                        CompanyKnowledgeReview.tenant_id == principal.tenant_id,
                        CompanyKnowledgeReview.proposal_id == proposal.id,
                        CompanyKnowledgeReview.subject_content_hash == subject_content_hash,
                    )
                    .order_by(CompanyKnowledgeReview.created_at, CompanyKnowledgeReview.id)
                )
            )
            .scalars()
            .all()
        )
        evaluation = evaluate_company_review_set(
            [
                {
                    "reviewer_user_id": row.reviewer_user_id,
                    "reviewer_role": row.reviewer_role,
                    "decision": row.decision,
                    "decision_hash": row.decision_hash,
                }
                for row in rows
            ],
            policy=dict(proposal.required_review_policy_json or {}),
            created_by_type=proposal.created_by_type,
            created_by_id=proposal.created_by_id,
            risk_level=proposal.risk_level,
        )
        if request.decision == "reject":
            proposal.status = next_company_proposal_status("in_review", "reject")
        elif request.decision == "request_changes":
            proposal.status = next_company_proposal_status("in_review", "request_changes")
        elif evaluation["approved"]:
            proposal.status = next_company_proposal_status("in_review", "approve")
        proposal.state_version += 1
        await append_company_knowledge_event(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.review_recorded",
                resource_type="proposal",
                resource_id=proposal.id,
                resource_version=proposal.state_version,
                source_refs=request.evidence_refs,
                source_hash=decision_hash,
                policy_snapshot={**policy, "review_evaluation": evaluation},
                trace_id=trace_id,
                idempotency_key=f"{proposal.idempotency_key}:review:{review.id}",
                outcome=proposal.status,
                payload={"decision": request.decision, "status": proposal.status},
            ),
        )
        return proposal

    async def publish_proposal(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        proposal_id: uuid.UUID,
        expected_state_version: int,
        valid_from: datetime,
        valid_until: datetime | None,
        trace_id: str,
    ) -> CompanyKnowledgePublication:
        proposal = await self._locked_proposal(
            session,
            tenant_id=principal.tenant_id,
            proposal_id=proposal_id,
            expected_state_version=expected_state_version,
        )
        if proposal.status != "approved":
            raise ValueError("approved_document_proposal_required")
        requires_materialization = self._requires_materialization(proposal)
        if requires_materialization and (
            proposal.materialized_document_id is None
            or not proposal.materialization_content_hash
            or not dict(proposal.materialization_receipt_json or {})
        ):
            raise ValueError("agent_proposed_update_materialization_required")
        materialization_receipt = (
            self._validated_materialization_receipt(proposal) if requires_materialization else None
        )
        document_id = proposal.materialized_document_id if requires_materialization else proposal.source_document_id
        if document_id is None:
            raise ValueError("approved_document_proposal_required")
        source = (
            await session.execute(
                select(CompanyKnowledgeSource).where(
                    CompanyKnowledgeSource.id == proposal.source_id,
                    CompanyKnowledgeSource.tenant_id == principal.tenant_id,
                )
            )
        ).scalar_one()
        document = (
            await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.id == document_id,
                    KnowledgeDocument.tenant_id == principal.tenant_id,
                    KnowledgeDocument.scope_type == "company",
                )
            )
        ).scalar_one()
        if requires_materialization:
            assert materialization_receipt is not None
            title_hash = hashlib.sha256(str(document.title).encode("utf-8")).hexdigest()
            if (
                document.source_sha256 != proposal.materialization_content_hash
                or document.artifact_hash != proposal.materialization_content_hash
                or document.canonical_md_sha256 != proposal.materialization_content_hash
                or title_hash != materialization_receipt["title_hash"]
            ):
                raise RuntimeError("company_knowledge_materialization_document_drift")
            try:
                materialized_artifact = _ensure_relative_to(
                    Path(document.canonical_md_path),
                    self._data_root,
                )
                artifact_hash = hashlib.sha256(materialized_artifact.read_bytes()).hexdigest()
            except (OSError, ValueError) as exc:
                raise RuntimeError("company_knowledge_materialization_artifact_unavailable") from exc
            if artifact_hash != proposal.materialization_content_hash:
                raise RuntimeError("company_knowledge_materialization_artifact_drift")
        resource = CompanyKnowledgeResource(
            tenant_id=principal.tenant_id,
            resource_type="company_knowledge_proposal",
            resource_id=proposal.id,
            resource_key=f"proposal:{proposal.id}",
            namespace=proposal.proposed_namespace,
            sensitivity=proposal.proposed_sensitivity,
            source_acl_snapshot_hash=source.source_acl_snapshot_hash,
            source_acl=dict(source.source_acl_snapshot_json),
            evidence_access_complete=bool(proposal.source_coverage_json.get("complete")),
            publication_status=None,
        )
        policy = await self._require_permission(
            session,
            principal=principal,
            resource=resource,
            action="publish",
        )
        reviews = (
            (
                await session.execute(
                    select(CompanyKnowledgeReview).where(
                        CompanyKnowledgeReview.tenant_id == principal.tenant_id,
                        CompanyKnowledgeReview.proposal_id == proposal.id,
                        CompanyKnowledgeReview.subject_content_hash == self._review_subject_hash(proposal),
                    )
                )
            )
            .scalars()
            .all()
        )
        evaluation = evaluate_company_review_set(
            [
                {
                    "reviewer_user_id": review.reviewer_user_id,
                    "reviewer_role": review.reviewer_role,
                    "decision": review.decision,
                    "decision_hash": review.decision_hash,
                }
                for review in reviews
            ],
            policy=dict(proposal.required_review_policy_json or {}),
            created_by_type=proposal.created_by_type,
            created_by_id=proposal.created_by_id,
            risk_level=proposal.risk_level,
        )
        if not evaluation["approved"] or not evaluation["review_set_hash"]:
            raise ValueError("company_knowledge_review_policy_not_satisfied")
        logical_key = f"{proposal.proposed_namespace}:{source.source_item_id}"
        current = (
            await session.execute(
                select(CompanyKnowledgePublication)
                .where(
                    CompanyKnowledgePublication.tenant_id == principal.tenant_id,
                    CompanyKnowledgePublication.logical_resource_key == logical_key,
                    CompanyKnowledgePublication.status == "active",
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        version = (
            await session.scalar(
                select(func.coalesce(func.max(CompanyKnowledgePublication.version), 0)).where(
                    CompanyKnowledgePublication.tenant_id == principal.tenant_id,
                    CompanyKnowledgePublication.logical_resource_key == logical_key,
                )
            )
        ) + 1
        now = _utcnow()
        if current is not None:
            current.status = "superseded"
            current.valid_until = valid_from
        proposal.status = next_company_proposal_status(proposal.status, "begin_publish")
        proposal.state_version += 1
        publication = CompanyKnowledgePublication(
            tenant_id=principal.tenant_id,
            logical_resource_key=logical_key,
            document_id=document.id,
            version=version,
            content_hash=document.source_sha256,
            artifact_ref=document.canonical_md_path,
            proposal_id=proposal.id,
            review_set_hash=evaluation["review_set_hash"],
            namespace=proposal.proposed_namespace,
            sensitivity=proposal.proposed_sensitivity,
            permission_resource_ref=f"company_knowledge_document:{document.id}",
            source_refs_json=list(proposal.source_refs_json or []),
            evidence_bundle_refs_json=list(proposal.source_refs_json or []),
            valid_from=valid_from,
            valid_until=valid_until,
            status="active",
            supersedes_publication_id=current.id if current else None,
            rollback_ref=f"company-publication://{logical_key}/version/{version - 1 if version > 1 else 0}",
            published_by_user_id=principal.accountable_user_id,
            published_at=now,
        )
        session.add(publication)
        await session.flush()
        proposal.status = next_company_proposal_status(proposal.status, "publish_succeeded")
        proposal.state_version += 1
        await append_company_knowledge_event_with_outbox(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.published",
                resource_type="publication",
                resource_id=publication.id,
                resource_version=publication.version,
                source_refs=tuple(publication.source_refs_json or []),
                source_hash=publication.content_hash,
                policy_snapshot={**policy, "review_set_hash": publication.review_set_hash},
                trace_id=trace_id,
                idempotency_key=f"{proposal.idempotency_key}:published:{publication.version}",
                outcome="active",
                payload={
                    "publication_id": str(publication.id),
                    "document_id": str(document.id),
                    "version": publication.version,
                },
                occurred_at=now,
            ),
            aggregate_type="publication",
            aggregate_id=publication.id,
            outbox_event_type="company_knowledge.publication_index_requested",
            outbox_idempotency_key=f"{proposal.idempotency_key}:publication-index:{publication.version}",
            outbox_payload={
                "operation": "index_document",
                "publication_id": str(publication.id),
                "document_id": str(document.id),
            },
            available_at=now,
        )
        return publication

    async def list_publication_lifecycle(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Project recoverable publication lifecycle rows for the control plane.

        Every row is re-authorized for its currently available lifecycle action
        before its title is disclosed. Forensic refs, hashes, policy snapshots,
        and source metadata stay on the audit/evidence surfaces.
        """

        bounded_limit = max(1, min(int(limit), 500))
        rows = (
            await session.execute(
                select(CompanyKnowledgePublication, KnowledgeDocument)
                .join(
                    KnowledgeDocument,
                    KnowledgeDocument.id == CompanyKnowledgePublication.document_id,
                )
                .where(
                    CompanyKnowledgePublication.tenant_id == principal.tenant_id,
                    CompanyKnowledgePublication.status.in_(("active", "retired")),
                )
                .order_by(
                    CompanyKnowledgePublication.published_at.desc(),
                    CompanyKnowledgePublication.version.desc(),
                    CompanyKnowledgePublication.id,
                )
                .limit(bounded_limit)
            )
        ).all()
        result: list[dict[str, Any]] = []
        for publication, document in rows:
            available_action = "restore" if publication.status == "retired" else "retire"
            resource = await self._publication_resource(session, publication)
            try:
                await self._require_permission(
                    session,
                    principal=principal,
                    resource=resource,
                    action=available_action,
                )
            except (LookupError, PermissionError):
                continue
            result.append(
                {
                    "publication_id": str(publication.id),
                    "document_id": str(publication.document_id),
                    "title": str(document.title or "Untitled knowledge"),
                    "status": publication.status,
                    "version": publication.version,
                    "namespace": publication.namespace,
                    "sensitivity": publication.sensitivity,
                    "valid_from": publication.valid_from,
                    "valid_until": publication.valid_until,
                    "available_action": available_action,
                }
            )
        return result

    async def retire_publication(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        publication_id: uuid.UUID,
        reason: str,
        trace_id: str,
    ) -> CompanyKnowledgePublication:
        if not reason.strip():
            raise ValueError("retirement_reason_required")
        publication = (
            await session.execute(
                select(CompanyKnowledgePublication)
                .where(
                    CompanyKnowledgePublication.id == publication_id,
                    CompanyKnowledgePublication.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if publication is None:
            raise LookupError("company_knowledge_publication_not_found")
        if publication.status != "active":
            raise ValueError("active_company_knowledge_publication_required")
        resource = await self._publication_resource(session, publication)
        policy = await self._require_permission(
            session,
            principal=principal,
            resource=resource,
            action="retire",
        )
        now = _utcnow()
        publication.status = "retired"
        publication.retired_at = now
        publication.valid_until = now
        await append_company_knowledge_event_with_outbox(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.retired",
                resource_type="publication",
                resource_id=publication.id,
                resource_version=publication.version,
                source_refs=tuple(publication.source_refs_json or []),
                source_hash=publication.content_hash,
                policy_snapshot=policy,
                trace_id=trace_id,
                idempotency_key=f"publication:{publication.id}:retired",
                outcome="retired",
                payload={"reason": reason, "publication_id": str(publication.id)},
                occurred_at=now,
            ),
            aggregate_type="publication",
            aggregate_id=publication.id,
            outbox_event_type="company_knowledge.publication_tombstone_requested",
            outbox_idempotency_key=f"publication:{publication.id}:tombstone",
            outbox_payload={
                "operation": "tombstone_publication",
                "publication_id": str(publication.id),
                "document_id": str(publication.document_id),
            },
            available_at=now,
        )
        return publication

    async def restore_publication(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        publication_id: uuid.UUID,
        reason: str,
        valid_from: datetime,
        trace_id: str,
    ) -> CompanyKnowledgePublication:
        if not reason.strip():
            raise ValueError("restore_reason_required")
        retired = (
            await session.execute(
                select(CompanyKnowledgePublication)
                .where(
                    CompanyKnowledgePublication.id == publication_id,
                    CompanyKnowledgePublication.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if retired is None:
            raise LookupError("company_knowledge_publication_not_found")
        if retired.status != "retired":
            raise ValueError("retired_company_knowledge_publication_required")
        resource = await self._publication_resource(session, retired)
        policy = await self._require_permission(
            session,
            principal=principal,
            resource=resource,
            action="restore",
        )
        active = (
            await session.execute(
                select(CompanyKnowledgePublication)
                .where(
                    CompanyKnowledgePublication.tenant_id == principal.tenant_id,
                    CompanyKnowledgePublication.logical_resource_key == retired.logical_resource_key,
                    CompanyKnowledgePublication.status == "active",
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        version = (
            await session.scalar(
                select(func.max(CompanyKnowledgePublication.version)).where(
                    CompanyKnowledgePublication.tenant_id == principal.tenant_id,
                    CompanyKnowledgePublication.logical_resource_key == retired.logical_resource_key,
                )
            )
        ) + 1
        if active is not None:
            active.status = "superseded"
            active.valid_until = valid_from
        now = _utcnow()
        restored = CompanyKnowledgePublication(
            tenant_id=retired.tenant_id,
            logical_resource_key=retired.logical_resource_key,
            document_id=retired.document_id,
            version=version,
            content_hash=retired.content_hash,
            artifact_ref=retired.artifact_ref,
            proposal_id=retired.proposal_id,
            review_set_hash=retired.review_set_hash,
            namespace=retired.namespace,
            sensitivity=retired.sensitivity,
            permission_resource_ref=retired.permission_resource_ref,
            source_refs_json=list(retired.source_refs_json or []),
            evidence_bundle_refs_json=list(retired.evidence_bundle_refs_json or []),
            valid_from=valid_from,
            valid_until=None,
            status="active",
            supersedes_publication_id=active.id if active else retired.id,
            rollback_ref=f"company-publication://{retired.logical_resource_key}/version/{retired.version}",
            published_by_user_id=principal.accountable_user_id,
            published_at=now,
            restored_from_publication_id=retired.id,
        )
        session.add(restored)
        await session.flush()
        await append_company_knowledge_event_with_outbox(
            session,
            event_input=_event_input(
                principal=principal,
                event_type="company_knowledge.restored",
                resource_type="publication",
                resource_id=restored.id,
                resource_version=restored.version,
                source_refs=tuple(restored.source_refs_json or []),
                source_hash=restored.content_hash,
                policy_snapshot=policy,
                trace_id=trace_id,
                idempotency_key=f"publication:{retired.id}:restored:{restored.version}",
                outcome="active",
                payload={"reason": reason, "restored_from_publication_id": str(retired.id)},
                occurred_at=now,
            ),
            aggregate_type="publication",
            aggregate_id=restored.id,
            outbox_event_type="company_knowledge.publication_restore_index_requested",
            outbox_idempotency_key=f"publication:{retired.id}:restore-index:{restored.version}",
            outbox_payload={
                "operation": "index_document",
                "publication_id": str(restored.id),
                "document_id": str(restored.document_id),
            },
            available_at=now,
        )
        return restored

    @staticmethod
    async def _publication_resource(
        session: Any,
        publication: CompanyKnowledgePublication,
    ) -> CompanyKnowledgeResource:
        proposal = (
            await session.execute(
                select(CompanyKnowledgeProposal).where(
                    CompanyKnowledgeProposal.id == publication.proposal_id,
                    CompanyKnowledgeProposal.tenant_id == publication.tenant_id,
                )
            )
        ).scalar_one()
        source = (
            await session.execute(
                select(CompanyKnowledgeSource).where(
                    CompanyKnowledgeSource.id == proposal.source_id,
                    CompanyKnowledgeSource.tenant_id == publication.tenant_id,
                )
            )
        ).scalar_one()
        return CompanyKnowledgeResource(
            tenant_id=publication.tenant_id,
            resource_type="company_knowledge_document",
            resource_id=publication.document_id,
            resource_key=f"document:{publication.document_id}",
            namespace=publication.namespace,
            sensitivity=publication.sensitivity,
            source_acl_snapshot_hash=source.source_acl_snapshot_hash,
            source_acl=dict(source.source_acl_snapshot_json),
            evidence_access_complete=bool(proposal.source_coverage_json.get("complete")),
            publication_status=publication.status,
            validity_active=True,
        )


__all__ = [
    "CompanyEvidenceIngestRequest",
    "CompanyKnowledgeImportRecoverySummary",
    "CompanyKnowledgeProposalRequest",
    "CompanyKnowledgeReviewRequest",
    "CompanyKnowledgeService",
]
