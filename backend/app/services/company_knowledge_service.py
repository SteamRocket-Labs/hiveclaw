"""Governed Company Knowledge source, ingest, review, and publication service.

The service owns authority transitions and durable evidence. It does not make
semantic decisions: source content and proposal patches are supplied by the
authenticated caller, while policy, ACL, lifecycle, idempotency, and recovery
remain deterministic platform responsibilities.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select

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
    SourceContractInput,
    build_canonical_evidence_envelope,
    company_knowledge_artifact_path,
    compute_source_contract_hash,
    evaluate_company_review_set,
    next_company_proposal_status,
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
    clean_title,
    normalize_markdown,
    segment_markdown,
)
from app.services.privacy_layer import canonicalize_sensitivity, sensitivity_rank


_EVIDENCE_KINDS = frozenset(
    {"document", "structured_record", "event", "living_object_revision", "external_immutable_ref"}
)
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
class CompanyKnowledgeImportRecoverySummary:
    attempted: int
    completed: int
    failed: int
    skipped: int
    job_refs: tuple[tuple[uuid.UUID, uuid.UUID], ...]


class CompanyKnowledgeService:
    """Company authority service shared by API handlers and background workers."""

    def __init__(self, *, data_root: str | Path) -> None:
        self._data_root = Path(data_root).expanduser().resolve()

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
        request_hash = _hash_json(
            {
                **_jsonable(asdict(request)),
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
            **_jsonable(asdict(request)),
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
                job.status = "failed"
                raise RuntimeError("company_knowledge_import_attempts_exhausted")
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
                payload = artifact.read_bytes()
                if hashlib.sha256(payload).hexdigest() != job.artifact_hash:
                    raise ValueError("company_knowledge_import_artifact_hash_mismatch")
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
                    markdown = payload.decode("utf-8")
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
                    failed.last_error_code = type(exc).__name__
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
                    CompanyKnowledgeImportJob.attempt_count < CompanyKnowledgeImportJob.max_attempts,
                    or_(
                        and_(
                            CompanyKnowledgeImportJob.status.in_(("queued", "failed")),
                            CompanyKnowledgeImportJob.available_at <= now,
                        ),
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
        if not request.reason.strip() or not request.evidence_refs:
            raise ValueError("review_reason_and_evidence_required")
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
        action = "approve" if request.decision == "approve" else "review"
        policy = await self._require_permission(
            session,
            principal=principal,
            resource=resource,
            action=action,
        )
        if proposal.status == "submitted":
            proposal.status = next_company_proposal_status(proposal.status, "begin_review")
        if proposal.status != "in_review":
            raise ValueError("company_knowledge_proposal_not_in_review")
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
                "policy_snapshot": request.policy_snapshot,
            }
        )
        review = CompanyKnowledgeReview(
            tenant_id=principal.tenant_id,
            proposal_id=proposal.id,
            reviewer_user_id=principal.accountable_user_id,
            reviewer_role=request.reviewer_role,
            review_round=review_round,
            decision=request.decision,
            reason=request.reason,
            evidence_refs_json=list(request.evidence_refs),
            policy_snapshot_json=dict(request.policy_snapshot),
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
        if proposal.status != "approved" or proposal.source_document_id is None:
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
                    KnowledgeDocument.id == proposal.source_document_id,
                    KnowledgeDocument.tenant_id == principal.tenant_id,
                    KnowledgeDocument.scope_type == "company",
                )
            )
        ).scalar_one()
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
