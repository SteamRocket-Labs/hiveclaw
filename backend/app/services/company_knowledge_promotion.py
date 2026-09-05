"""Explicit Personal/legacy intake into the governed Company review lane."""

from __future__ import annotations

import hashlib
import mimetypes
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
import stat as stat_module
import tempfile
from typing import Any
import uuid

import anyio
from sqlalchemy import select, text

from app.models.company_knowledge import CompanyKnowledgeImportJob, CompanyKnowledgeProposal
from app.models.knowledge import KnowledgeDocument
from app.services.company_knowledge_contracts import (
    CompanyKnowledgePromotionHandoff,
    SourceContractInput,
    validate_company_knowledge_promotion_handoff,
)
from app.services.company_knowledge_evidence import (
    CompanyKnowledgeEventInput,
    append_company_knowledge_event,
)
from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal
from app.services.company_knowledge_service import (
    CompanyEvidenceIngestRequest,
    CompanyKnowledgeService,
)
from app.services.legacy_company_files import read_legacy_company_file
from app.services.personal_knowledge_access import HumanBrowserPrincipal
from app.services.personal_knowledge_ingest import clean_title, normalize_markdown
from app.services.personal_knowledge_service import PersonalKnowledgeService
from app.services.privacy_layer import (
    SensitivityLevel,
    canonicalize_sensitivity,
)


_MAX_PROMOTION_SOURCE_BYTES = 50 * 1024 * 1024
_PROMOTION_KINDS = frozenset({"personal_promotion", "legacy_import"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class PersonalPromotionIntakeRequest:
    document_id: uuid.UUID
    proposed_namespace: str
    purpose: str
    risk_level: str
    title: str | None
    attest_scope_change: bool
    idempotency_key: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class LegacyPromotionIntakeRequest:
    relative_path: str
    expected_sha256: str
    proposed_namespace: str
    proposed_sensitivity: str
    purpose: str
    risk_level: str
    title: str | None
    attest_scope_change: bool
    idempotency_key: str
    trace_id: str


def _required_text(value: str, *, field: str, max_length: int) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{field}_required")
    if len(clean) > max_length:
        raise ValueError(f"{field}_too_long")
    return clean


def _namespace(value: str) -> str:
    clean = _required_text(value, field="proposed_namespace", max_length=300).strip("/")
    if clean.startswith(".") or "//" in clean or any(part in {"", ".", ".."} for part in clean.split("/")):
        raise ValueError("company_knowledge_promotion_namespace_invalid")
    return clean


def _validate_common_request(
    *,
    proposed_namespace: str,
    purpose: str,
    risk_level: str,
    attest_scope_change: bool,
    idempotency_key: str,
    trace_id: str,
) -> tuple[str, str, str, str]:
    if attest_scope_change is not True:
        raise ValueError("company_knowledge_promotion_scope_change_attestation_required")
    if risk_level not in {"normal", "high", "critical"}:
        raise ValueError("unsupported_company_knowledge_risk_level")
    return (
        _namespace(proposed_namespace),
        _required_text(purpose, field="purpose", max_length=1000),
        _required_text(idempotency_key, field="idempotency_key", max_length=200),
        _required_text(trace_id, field="trace_id", max_length=300),
    )


def _source_acl(*, user_id: uuid.UUID) -> dict[str, Any]:
    return {
        "user_ids": [str(user_id)],
        "role_names": ["org_admin"],
        "scope_change_attested": True,
    }


def _read_verified_artifact(
    *,
    data_root: Path,
    artifact_path: Path,
    expected_sha256: str,
    required_root: Path | None = None,
) -> bytes:
    root = data_root.expanduser().resolve()
    allowed_root = (required_root or root).expanduser().resolve()
    try:
        allowed_root.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("company_knowledge_promotion_artifact_unavailable") from exc
    candidate = artifact_path if artifact_path.is_absolute() else root / artifact_path
    candidate = Path(os.path.abspath(os.fspath(candidate)))
    try:
        candidate.relative_to(root)
        relative = candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise RuntimeError("company_knowledge_promotion_artifact_unavailable") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise RuntimeError("company_knowledge_promotion_artifact_unavailable")

    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        if os.open in os.supports_dir_fd:
            directory_descriptor = os.open(allowed_root, directory_flags)
            try:
                for part in relative.parts[:-1]:
                    next_descriptor = os.open(
                        part,
                        directory_flags,
                        dir_fd=directory_descriptor,
                    )
                    os.close(directory_descriptor)
                    directory_descriptor = next_descriptor
                descriptor = os.open(
                    relative.parts[-1],
                    file_flags,
                    dir_fd=directory_descriptor,
                )
            finally:
                os.close(directory_descriptor)
        else:  # pragma: no cover - Windows fallback for local development
            current = allowed_root
            for part in relative.parts:
                current = current / part
                if stat_module.S_ISLNK(current.lstat().st_mode):
                    raise RuntimeError("company_knowledge_promotion_artifact_unavailable")
            descriptor = os.open(candidate, file_flags)
    except OSError as exc:
        raise RuntimeError("company_knowledge_promotion_artifact_unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat_module.S_ISREG(before.st_mode):
            raise RuntimeError("company_knowledge_promotion_artifact_unavailable")
        if before.st_size > _MAX_PROMOTION_SOURCE_BYTES:
            raise ValueError("company_knowledge_promotion_source_too_large")
        content = bytearray()
        hasher = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            while chunk := source.read(1024 * 1024):
                content.extend(chunk)
                hasher.update(chunk)
            after = os.fstat(source.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    expected = str(expected_sha256 or "").strip().lower()
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or len(content) != before.st_size
        or hasher.hexdigest() != expected
    ):
        raise RuntimeError("company_knowledge_promotion_artifact_changed")
    return bytes(content)


def _promotion_source_contract(
    *,
    principal: CompanyKnowledgePrincipal,
    proposal_kind: str,
    stable_source_id: str,
    owner_principal_ref: str,
    namespace: str,
    sensitivity: str,
) -> SourceContractInput:
    if proposal_kind not in _PROMOTION_KINDS:
        raise ValueError("unsupported_company_knowledge_promotion_kind")
    return SourceContractInput(
        source_kind=proposal_kind,
        provider_kind="hive_native",
        stable_source_id=stable_source_id,
        owner_principal_ref=owner_principal_ref,
        accountable_steward_ref=f"user:{principal.accountable_user_id}",
        connection_ref=None,
        schema_ref="schema://hive/company-knowledge-promotion/v1",
        schema_version="1",
        identity_keys=("source_item_id", "source_revision"),
        relation_keys=(),
        ingest_mode="snapshot",
        cursor_kind=None,
        cursor_policy={},
        watermark_field=None,
        temporal_mapping={"observed_at": "promotion_intake_time"},
        source_acl_mapping_policy={
            "mode": "explicit_scope_change",
            "required_attestation": True,
        },
        default_sensitivity=sensitivity,
        export_policy={"allowed": False},
        retention_policy={"class": "company_review_evidence"},
        legal_hold_policy={"supported": True},
        allowed_namespaces=(namespace,),
        precedence_policy_ref=None,
        acceptance_suite_ref="acceptance://hive/company-knowledge-promotion/v1",
        idempotency_policy={"key": "source_item_id+source_revision"},
    )


class CompanyKnowledgePromotionService:
    """Create evidence first and let the import transaction submit the proposal."""

    def __init__(
        self,
        *,
        data_root: str | Path,
        company_service: CompanyKnowledgeService | Any | None = None,
        personal_service: PersonalKnowledgeService | Any | None = None,
        conversion_service: Any | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.company_service = company_service or CompanyKnowledgeService(data_root=self.data_root)
        self.personal_service = personal_service or PersonalKnowledgeService(data_root=self.data_root)
        self.conversion_service = conversion_service

    def _conversion_service(self) -> Any:
        if self.conversion_service is not None:
            return self.conversion_service
        from app.services.document_conversion import DocumentConversionService

        return DocumentConversionService(max_file_bytes=_MAX_PROMOTION_SOURCE_BYTES)

    async def _queue(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        proposal_kind: str,
        stable_source_id: str,
        owner_principal_ref: str,
        source_item_id: str,
        source_revision: str,
        title: str,
        markdown: str,
        namespace: str,
        sensitivity: str,
        purpose: str,
        risk_level: str,
        original_source_ref: str,
        original_source_label: str,
        conversion_receipt: dict[str, Any],
        idempotency_key: str,
        trace_id: str,
    ) -> CompanyKnowledgeImportJob:
        if principal.actor_type != "user":
            raise PermissionError("agents_cannot_promote_personal_or_legacy_knowledge")
        normalized = normalize_markdown(markdown)
        if not normalized:
            raise ValueError("company_knowledge_promotion_candidate_empty")
        candidate_hash = _sha256_text(normalized)
        promotion_key = (
            f"company-promotion:"
            f"{'personal' if proposal_kind == 'personal_promotion' else 'legacy'}:"
            f"{principal.accountable_user_id}"
        )
        job_idempotency_key = f"{promotion_key}:{idempotency_key}"
        handoff = CompanyKnowledgePromotionHandoff(
            proposal_kind=proposal_kind,
            title=clean_title(title),
            original_source_ref=original_source_ref,
            original_source_label=original_source_label,
            source_revision_ref=source_revision,
            proposed_namespace=namespace,
            proposed_sensitivity=sensitivity,
            risk_level=risk_level,
            purpose=purpose,
            candidate_content_hash=candidate_hash,
            explicit_scope_change_attested=True,
            proposal_idempotency_key=f"{promotion_key}:{idempotency_key}:proposal",
            trace_id=trace_id,
            conversion_receipt=dict(conversion_receipt),
        )
        validate_company_knowledge_promotion_handoff(
            handoff,
            artifact_hash=candidate_hash,
            markdown=normalized,
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": (f"company-knowledge-import:{principal.tenant_id}:{job_idempotency_key}")},
        )
        existing = (
            await session.execute(
                select(CompanyKnowledgeImportJob)
                .where(
                    CompanyKnowledgeImportJob.tenant_id == principal.tenant_id,
                    CompanyKnowledgeImportJob.idempotency_key == job_idempotency_key,
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            stored = dict(existing.request_json or {})
            if (
                stored.get("artifact_hash") == candidate_hash
                and stored.get("source_item_id") == source_item_id
                and stored.get("source_revision") == source_revision
                and stored.get("promotion_handoff") == asdict(handoff)
            ):
                return existing
            raise ValueError("company_knowledge_promotion_idempotency_conflict")

        contract_input = _promotion_source_contract(
            principal=principal,
            proposal_kind=proposal_kind,
            stable_source_id=stable_source_id,
            owner_principal_ref=owner_principal_ref,
            namespace=namespace,
            sensitivity=sensitivity,
        )
        contract_key = hashlib.sha256(f"{stable_source_id}:{namespace}:{sensitivity}".encode("utf-8")).hexdigest()
        contract = await self.company_service.register_source_contract(
            session,
            principal=principal,
            contract_input=contract_input,
            idempotency_key=f"company-promotion-contract:{contract_key}",
            trace_id=trace_id,
        )
        return await self.company_service.queue_evidence_import(
            session,
            principal=principal,
            request=CompanyEvidenceIngestRequest(
                source_contract_id=contract.id,
                source_contract_version=contract.version,
                evidence_kind="document",
                source_item_id=source_item_id,
                source_revision=source_revision,
                title=handoff.title,
                markdown=normalized,
                typed_payload=None,
                external_artifact_ref=None,
                schema_ref="schema://hive/company-knowledge-promotion/v1",
                source_acl_snapshot=_source_acl(user_id=principal.accountable_user_id),
                proposed_namespace=namespace,
                proposed_sensitivity=sensitivity,
                occurred_at=None,
                effective_from=None,
                effective_until=None,
                observed_at=_utcnow(),
                cursor={},
                sequence=None,
                coverage_ledger={
                    "complete": True,
                    "total_units": 1,
                    "covered_units": 1,
                    "missing_units": [],
                },
                purpose=purpose,
                idempotency_key=job_idempotency_key,
                trace_id=trace_id,
                promotion_handoff=handoff,
            ),
        )

    async def queue_personal_promotion(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: PersonalPromotionIntakeRequest,
    ) -> CompanyKnowledgeImportJob:
        namespace, purpose, idempotency_key, trace_id = _validate_common_request(
            proposed_namespace=request.proposed_namespace,
            purpose=request.purpose,
            risk_level=request.risk_level,
            attest_scope_change=request.attest_scope_change,
            idempotency_key=request.idempotency_key,
            trace_id=request.trace_id,
        )
        if principal.actor_type != "user":
            raise PermissionError("agents_cannot_promote_personal_or_legacy_knowledge")
        owner_user_id = principal.accountable_user_id
        result = await self.personal_service.get_personal_document_with_authority(
            session,
            tenant_id=principal.tenant_id,
            owner_user_id=owner_user_id,
            document_id=request.document_id,
            principal=HumanBrowserPrincipal(user_id=owner_user_id),
        )
        if result.status == "denied":
            raise PermissionError("personal_knowledge_promotion_source_denied")
        if result.status == "empty":
            raise LookupError("personal_knowledge_promotion_source_not_found")
        if result.status != "ok" or result.document is None:
            raise RuntimeError("personal_knowledge_promotion_source_unavailable")
        document = result.document
        sensitivity = canonicalize_sensitivity(document.sensitivity)
        if sensitivity == SensitivityLevel.PL4_CREDENTIAL:
            raise PermissionError("credential_content_cannot_be_promoted_to_company_knowledge")

        canonical_hash = str(getattr(document, "canonical_md_sha256", "") or "").strip().lower()
        canonical_path = str(getattr(document, "canonical_md_path", "") or "").strip()
        if not canonical_hash:
            row = (
                await session.execute(
                    select(
                        KnowledgeDocument.canonical_md_path,
                        KnowledgeDocument.canonical_md_sha256,
                    ).where(
                        KnowledgeDocument.id == request.document_id,
                        KnowledgeDocument.tenant_id == principal.tenant_id,
                        KnowledgeDocument.scope_type == "person",
                        KnowledgeDocument.scope_id == owner_user_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise LookupError("personal_knowledge_promotion_source_not_found")
            canonical_path = str(row[0] or "")
            canonical_hash = str(row[1] or "").lower()
        if not canonical_path or len(canonical_hash) != 64:
            raise RuntimeError("personal_knowledge_promotion_artifact_unavailable")
        personal_root = self.data_root / "persons" / str(owner_user_id) / "kb"
        payload = await anyio.to_thread.run_sync(
            partial(
                _read_verified_artifact,
                data_root=self.data_root,
                artifact_path=Path(canonical_path),
                expected_sha256=canonical_hash,
                required_root=personal_root,
            )
        )
        try:
            markdown = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("personal_knowledge_promotion_artifact_unavailable") from exc
        source_ref = str(
            getattr(document, "source_ref", "") or f"kb://person/{owner_user_id}/documents/{request.document_id}"
        )
        return await self._queue(
            session,
            principal=principal,
            proposal_kind="personal_promotion",
            stable_source_id=f"hive-personal-promotion:{owner_user_id}:{namespace}",
            owner_principal_ref=f"user:{owner_user_id}",
            source_item_id=f"personal-document:{request.document_id}",
            source_revision=canonical_hash,
            title=request.title or str(document.title),
            markdown=markdown,
            namespace=namespace,
            sensitivity=sensitivity.value,
            purpose=purpose,
            risk_level=request.risk_level,
            original_source_ref=source_ref,
            original_source_label=str(document.title),
            conversion_receipt={},
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def queue_legacy_promotion(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        company_dir: Path,
        request: LegacyPromotionIntakeRequest,
    ) -> CompanyKnowledgeImportJob:
        namespace, purpose, idempotency_key, trace_id = _validate_common_request(
            proposed_namespace=request.proposed_namespace,
            purpose=request.purpose,
            risk_level=request.risk_level,
            attest_scope_change=request.attest_scope_change,
            idempotency_key=request.idempotency_key,
            trace_id=request.trace_id,
        )
        # PDEC-013: both scoped administrator roles are accountable for legacy
        # promotion inside the company their request resolved to; the API layer
        # builds this principal only after tenant scoping, so the platform
        # administrator here is already inside the selected company. Attestation,
        # evidence, source-path and credential gates below are unchanged.
        if principal.actor_type != "user" or principal.accountable_role not in ("org_admin", "platform_admin"):
            raise PermissionError("legacy_company_promotion_requires_tenant_admin")
        sensitivity = canonicalize_sensitivity(request.proposed_sensitivity).value
        try:
            selected = await anyio.to_thread.run_sync(
                partial(
                    read_legacy_company_file,
                    company_dir,
                    relative_path=request.relative_path,
                    expected_sha256=request.expected_sha256,
                    max_bytes=_MAX_PROMOTION_SOURCE_BYTES,
                )
            )
        except RuntimeError:
            raise
        except Exception as exc:
            if type(exc).__name__ == "LegacyCompanyFilesChangedError":
                raise RuntimeError("legacy_company_promotion_source_changed") from exc
            raise
        with tempfile.TemporaryDirectory(prefix="hive-company-promotion-") as conversion_workspace:
            conversion = await anyio.to_thread.run_sync(
                partial(
                    self._conversion_service().convert_bytes,
                    data=selected.data,
                    filename=Path(selected.item.relative_path).name,
                    workspace_root=Path(conversion_workspace),
                    source_uri=f"legacy-company-file://{selected.item.sha256}",
                    source_mime_type=(
                        mimetypes.guess_type(selected.item.relative_path)[0] or "application/octet-stream"
                    ),
                    tenant_id=principal.tenant_id,
                    agent_id=None,
                    user_id=principal.accountable_user_id,
                    mode="auto",
                    force_refresh=False,
                )
            )
        markdown = normalize_markdown(str(getattr(conversion, "markdown", "") or ""))
        if not markdown:
            raise RuntimeError("legacy_company_promotion_conversion_empty")
        candidate_hash = _sha256_text(markdown)
        return await self._queue(
            session,
            principal=principal,
            proposal_kind="legacy_import",
            stable_source_id=f"hive-legacy-promotion:{principal.tenant_id}:{namespace}",
            owner_principal_ref=f"tenant:{principal.tenant_id}",
            source_item_id=f"legacy-company-file:{selected.item.sha256}",
            source_revision=f"{selected.item.sha256}:{candidate_hash}",
            title=request.title or Path(selected.item.relative_path).stem,
            markdown=markdown,
            namespace=namespace,
            sensitivity=sensitivity,
            purpose=purpose,
            risk_level=request.risk_level,
            original_source_ref=f"legacy-company-file://{selected.item.sha256}",
            original_source_label=selected.item.relative_path,
            conversion_receipt={
                "engine": str(getattr(conversion, "engine", "unknown")),
                "source_mime_type": str(getattr(conversion, "source_mime_type", "application/octet-stream")),
                "warnings": list(getattr(conversion, "warnings", ()) or ()),
            },
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    @staticmethod
    def _intake_view(
        job: CompanyKnowledgeImportJob,
        *,
        proposal: CompanyKnowledgeProposal | None,
    ) -> dict[str, Any]:
        handoff = dict(job.request_json or {}).get("promotion_handoff")
        if not isinstance(handoff, dict) or not handoff:
            raise LookupError("company_knowledge_promotion_intake_not_found")
        if job.status == "completed" and job.proposal_id is not None:
            business_status = "ready_for_review"
            recovery = "none"
        elif job.status == "running":
            business_status = "processing"
            recovery = "automatic"
        elif job.status == "queued":
            business_status = "queued"
            recovery = "automatic"
        elif job.status == "failed" and job.attempt_count >= job.max_attempts:
            business_status = "retry_required"
            recovery = "manual"
        elif job.status == "failed":
            business_status = "retry_scheduled"
            recovery = "automatic"
        else:
            business_status = "held"
            recovery = "manual"
        return {
            "intake_id": str(job.id),
            "kind": str(handoff.get("proposal_kind") or ""),
            "title": str(handoff.get("title") or ""),
            "source_label": str(handoff.get("original_source_label") or ""),
            "namespace": str(handoff.get("proposed_namespace") or ""),
            "sensitivity": str(handoff.get("proposed_sensitivity") or ""),
            "status": business_status,
            "recovery": recovery,
            "attempt_count": int(job.attempt_count or 0),
            "proposal_id": str(job.proposal_id) if job.proposal_id else None,
            "proposal_status": str(proposal.status) if proposal is not None else None,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }

    async def _owned_job(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        job_id: uuid.UUID,
        for_update: bool = False,
    ) -> CompanyKnowledgeImportJob:
        statement = select(CompanyKnowledgeImportJob).where(
            CompanyKnowledgeImportJob.id == job_id,
            CompanyKnowledgeImportJob.tenant_id == principal.tenant_id,
            CompanyKnowledgeImportJob.accountable_user_id == principal.accountable_user_id,
            CompanyKnowledgeImportJob.request_json.op("?")("promotion_handoff"),
        )
        if for_update:
            statement = statement.with_for_update()
        job = (await session.execute(statement)).scalar_one_or_none()
        if job is None:
            raise LookupError("company_knowledge_promotion_intake_not_found")
        return job

    async def list_intakes(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        kind: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if kind is not None and kind not in _PROMOTION_KINDS:
            raise ValueError("unsupported_company_knowledge_promotion_kind")
        statement = (
            select(CompanyKnowledgeImportJob)
            .where(
                CompanyKnowledgeImportJob.tenant_id == principal.tenant_id,
                CompanyKnowledgeImportJob.accountable_user_id == principal.accountable_user_id,
                CompanyKnowledgeImportJob.request_json.op("?")("promotion_handoff"),
            )
            .order_by(
                CompanyKnowledgeImportJob.created_at.desc(),
                CompanyKnowledgeImportJob.id,
            )
            .limit(max(1, min(int(limit), 200)))
        )
        if kind is not None:
            statement = statement.where(
                CompanyKnowledgeImportJob.request_json["promotion_handoff"]["proposal_kind"].astext == kind
            )
        jobs = (await session.execute(statement)).scalars().all()
        result: list[dict[str, Any]] = []
        for job in jobs:
            proposal = await session.get(CompanyKnowledgeProposal, job.proposal_id) if job.proposal_id else None
            result.append(self._intake_view(job, proposal=proposal))
        return result

    async def get_intake(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        job_id: uuid.UUID,
    ) -> dict[str, Any]:
        job = await self._owned_job(
            session,
            principal=principal,
            job_id=job_id,
        )
        proposal = await session.get(CompanyKnowledgeProposal, job.proposal_id) if job.proposal_id else None
        return self._intake_view(job, proposal=proposal)

    async def retry_intake(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        job_id: uuid.UUID,
        trace_id: str,
    ) -> dict[str, Any]:
        job = await self._owned_job(
            session,
            principal=principal,
            job_id=job_id,
            for_update=True,
        )
        if job.status == "completed":
            proposal = await session.get(CompanyKnowledgeProposal, job.proposal_id) if job.proposal_id else None
            return self._intake_view(job, proposal=proposal)
        now = _utcnow()
        if job.status == "running" and job.claim_expires_at and job.claim_expires_at > now:
            raise RuntimeError("company_knowledge_promotion_intake_already_processing")
        if job.attempt_count >= job.max_attempts:
            job.max_attempts = job.attempt_count + 3
        job.status = "queued"
        job.available_at = now
        job.claim_token = None
        job.claim_expires_at = None
        await append_company_knowledge_event(
            session,
            event_input=CompanyKnowledgeEventInput(
                tenant_id=principal.tenant_id,
                event_type="company_knowledge.promotion_retry_requested",
                actor_type=principal.actor_type,
                actor_id=principal.actor_id,
                accountable_user_id=principal.accountable_user_id,
                resource_type="import_job",
                resource_id=job.id,
                resource_version=job.attempt_count,
                source_refs=(),
                source_hash=job.artifact_hash,
                policy_snapshot={"authority": "promotion_intake_creator"},
                trace_id=_required_text(trace_id, field="trace_id", max_length=300),
                request_id=None,
                idempotency_key=f"{job.idempotency_key}:retry:{job.attempt_count}",
                outcome="queued",
                payload={"status": "queued", "attempt_count": job.attempt_count},
                occurred_at=now,
            ),
        )
        # PostgreSQL applies ``updated_at`` through the import-job server
        # onupdate expression. Flush and reload before synchronous response
        # serialization so an expired ORM attribute cannot trigger async IO.
        await session.flush()
        await session.refresh(job)
        proposal = await session.get(CompanyKnowledgeProposal, job.proposal_id) if job.proposal_id else None
        return self._intake_view(job, proposal=proposal)

    async def get_candidate(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        job_id: uuid.UUID,
    ) -> dict[str, Any]:
        job = (
            await session.execute(
                select(CompanyKnowledgeImportJob).where(
                    CompanyKnowledgeImportJob.id == job_id,
                    CompanyKnowledgeImportJob.tenant_id == principal.tenant_id,
                    CompanyKnowledgeImportJob.request_json.op("?")("promotion_handoff"),
                )
            )
        ).scalar_one_or_none()
        if job is None or job.proposal_id is None:
            raise LookupError("company_knowledge_promotion_candidate_not_found")
        proposal = await session.get(CompanyKnowledgeProposal, job.proposal_id)
        if proposal is None:
            raise LookupError("company_knowledge_promotion_candidate_not_found")
        await self.company_service.authorize_proposal_action(
            session,
            principal=principal,
            proposal=proposal,
            action="review",
        )
        handoff = CompanyKnowledgePromotionHandoff(**dict(dict(job.request_json or {})["promotion_handoff"]))
        payload = await anyio.to_thread.run_sync(
            partial(
                _read_verified_artifact,
                data_root=self.data_root,
                artifact_path=Path(str(job.artifact_ref or "")),
                expected_sha256=job.artifact_hash,
            )
        )
        try:
            markdown = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("company_knowledge_promotion_artifact_unavailable") from exc
        validate_company_knowledge_promotion_handoff(
            handoff,
            artifact_hash=job.artifact_hash,
            markdown=markdown,
        )
        return {
            "intake_id": str(job.id),
            "proposal_id": str(proposal.id),
            "title": handoff.title,
            "markdown": markdown,
            "content_hash": handoff.candidate_content_hash,
            "source_label": handoff.original_source_label,
            "namespace": handoff.proposed_namespace,
            "sensitivity": handoff.proposed_sensitivity,
        }


__all__ = [
    "CompanyKnowledgePromotionService",
    "LegacyPromotionIntakeRequest",
    "PersonalPromotionIntakeRequest",
]
