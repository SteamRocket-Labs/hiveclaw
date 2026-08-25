"""Personal Knowledge Base ingestion and search primitives."""

from __future__ import annotations

import asyncio
import inspect
import json
import mimetypes
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import Text, cast, delete, func, or_, select, update

from app.config import get_settings
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.knowledge import (
    KnowledgeAssertion,
    KnowledgeDocument,
    KnowledgeEntity,
    KnowledgeGrant,
    KnowledgeIndexJob,
    KnowledgeLink,
    KnowledgeSegment,
)
from app.models.user import User
from app.services.personal_knowledge_access import (
    AgentRuntimePrincipal,
    PersonalKnowledgePermissionDecision,
    PersonalKnowledgePrincipal,
    _personal_knowledge_access_predicate,
    _personal_knowledge_agent_visibility_predicate,
    build_personal_knowledge_document_list_statement,
    personal_knowledge_consumable_status_predicate,
    resolve_personal_knowledge_permission,
)
from app.services.personal_knowledge_index_search import (
    _clean_graph_text,
    _coerce_confidence,
    _escape_like,
    _freshness_boost,
    _heat_boost,
    _merge_source_refs,
    _merge_unique_strings,
    _row_first,
    _source_ref_segment_ids,
    build_personal_knowledge_search_statement,
)
from app.services.personal_knowledge_ingest import (
    _SUPPORTED_IMPORT_EXTENSIONS,
    _WHITESPACE_RE,
    _clean_title,
    _extension_for_filename,
    _extract_semaphore_for_tenant,
    _finalize_extraction_usage,
    _media_kind_for_extension,
    _new_extraction_usage_summary,
    _normalize_markdown,
    _personal_knowledge_root,
    _record_extraction_usage,
    _safe_filename,
    _sha256,
    _sha256_bytes,
    _validate_source_sha256,
    KnowledgeSegmentDraft as KnowledgeSegmentDraft,
    personal_knowledge_artifact_path,
    personal_knowledge_import_spool_path,
    segment_markdown,
    title_from_filename_or_uri,
)
from app.services.personal_knowledge_jobs import (
    DEFAULT_IMPORT_JOB_MAX_ATTEMPTS as _DEFAULT_IMPORT_JOB_MAX_ATTEMPTS,
    build_personal_knowledge_job_claim_statement,
)
from app.services.privacy_layer import SensitivityLevel, canonicalize_sensitivity, is_sensitive_extraction_blocked


_DEFAULT_EXTRACTOR = object()
_DEFAULT_IMPORT_JOB_QUEUED_GRACE_SECONDS = 0
_DEFAULT_IMPORT_JOB_RUNNING_TIMEOUT_SECONDS = 600
_CREDENTIAL_REFERENCE_KEYS = ("credential_reference", "credential_ref", "secret_reference", "secret_ref")
_CREDENTIAL_REFERENCE_PREFIXES = ("secret://", "credential://", "vault://")
_PERSONAL_AGENT_GRANT_PURPOSES = frozenset(
    {"interactive_session", "autonomous_agent", "a2a_delegation", "subagent_delegation"}
)
_PERSONAL_DELEGATED_GRANT_PURPOSES = frozenset({"a2a_delegation", "subagent_delegation"})


def _credential_reference_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    """Return an opaque Secret Store reference; raw URLs, paths, and values are never accepted."""

    values = dict(metadata or {})
    for key in _CREDENTIAL_REFERENCE_KEYS:
        candidate = str(values.get(key) or "").strip()
        if not candidate or len(candidate) > 512:
            continue
        if any(char.isspace() or ord(char) < 32 for char in candidate):
            continue
        if candidate.lower().startswith(_CREDENTIAL_REFERENCE_PREFIXES):
            return candidate
    return None


@dataclass(frozen=True)
class PersonalKnowledgeIngestResult:
    document_id: uuid.UUID
    job_id: uuid.UUID | None
    source_sha256: str
    artifact_hash: str
    canonical_md_path: str
    segment_count: int
    status: str
    warnings: list[str]
    error_code: str | None = None


@dataclass(frozen=True)
class PersonalKnowledgeDocumentSummary:
    document_id: uuid.UUID
    title: str
    source_kind: str
    source_uri: str | None
    source_sha256: str
    source_ref: str
    canonical_md_path: str
    status: str
    sensitivity: str
    agent_searchable: bool
    segment_count: int
    created_at: Any
    updated_at: Any
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PersonalKnowledgeDocumentSegment:
    segment_id: uuid.UUID
    position: int
    heading_path: list[str]
    content: str
    token_count: int


@dataclass(frozen=True)
class PersonalKnowledgeDocumentDetail(PersonalKnowledgeDocumentSummary):
    segments: list[PersonalKnowledgeDocumentSegment]


@dataclass(frozen=True)
class PersonalKnowledgeSourcePreview:
    filename: str
    mime_type: str
    content: bytes


@dataclass(frozen=True)
class KnowledgeSearchHit:
    document_id: uuid.UUID
    segment_id: uuid.UUID
    title: str
    snippet: str
    source_ref: str
    score: float
    heading_path: list[str]
    sensitivity: str
    metadata: dict[str, Any]
    score_trace: dict[str, Any] = field(default_factory=dict)
    credential_reference: str | None = None


@dataclass(frozen=True)
class PersonalKnowledgeSearchResult:
    status: str
    hits: list[KnowledgeSearchHit]
    authority: PersonalKnowledgePermissionDecision
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PersonalKnowledgeDocumentReadResult:
    status: str
    document: PersonalKnowledgeDocumentDetail | None
    credential_reference: str | None
    authority: PersonalKnowledgePermissionDecision
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PersonalKnowledgeJobSummary:
    job_id: uuid.UUID
    document_id: uuid.UUID
    stage: str
    status: str
    artifact_hash: str
    error_message: str | None
    attempt_count: int
    metadata: dict[str, Any]
    created_at: Any
    updated_at: Any
    terminal: bool = False
    retryable: bool = False
    cancellable: bool = False
    error_code: str | None = None
    max_attempts: int = _DEFAULT_IMPORT_JOB_MAX_ATTEMPTS
    lifecycle_status: str = ""
    result_status: str | None = None
    cancelled_at: str | None = None


@dataclass(frozen=True)
class PersonalKnowledgeJobProcessSummary:
    attempted: int
    succeeded: int
    failed: int
    skipped: int
    results: list[dict[str, Any]]


class PersonalKnowledgeJobConflict(Exception):
    """Typed lifecycle conflict (cancel/retry) with an exact machine code."""

    def __init__(self, code: str, *, cancellable: bool | None = None, retryable: bool | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.cancellable = cancellable
        self.retryable = retryable


class PersonalKnowledgeDocumentConflict(Exception):
    """Typed document lifecycle conflict (restore) with an exact machine code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PersonalKnowledgeImportError(Exception):
    """Typed import-pipeline failure carrying one exact machine code.

    The code is the user-facing error contract; the exception class name stays
    available as operator evidence, and raw exception prose never becomes UI
    state.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PersonalKnowledgeConversionError(PersonalKnowledgeImportError):
    """Conversion boundary failure (parser/provider), e.g. conversion_failed."""


class PersonalKnowledgeSourceMissingError(PersonalKnowledgeImportError):
    """The spooled source evidence for a queued import is no longer readable."""


class PersonalKnowledgeClaimLost(Exception):
    """The phase-1 claim lease (token + attempt + running) no longer matches.

    Raised when another worker reclaimed the stale lease; the losing worker
    must roll back every staged document/segment write and report the typed
    claim_lost outcome instead of a failure.
    """

    code = "claim_lost"

    def __init__(self, claimed_token: str) -> None:
        super().__init__("claim_lost")
        self.claimed_token = claimed_token


def _typed_import_error_code(exc: Exception) -> str:
    """Map an exception to its exact stable machine code.

    Typed pipeline failures carry their code; anything else collapses to the
    one generic safe code — no message-text classification.
    """

    if isinstance(exc, PersonalKnowledgeImportError):
        return exc.code
    return "import_failed"


# Lifecycle view derived from the durable job row (no schema change):
# lifecycle_status is queued/running/completed/failed/cancelled; result_status
# is ready/degraded/failed/cancelled where a terminal result exists. The raw
# status column stays only for compatibility.
_JOB_LIFECYCLE_BY_RAW_STATUS = {
    "queued": "queued",
    "running": "running",
    "ready": "completed",
    "degraded": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}
_JOB_RESULT_BY_RAW_STATUS = {
    "ready": "ready",
    "degraded": "degraded",
    "failed": "failed",
    "cancelled": "cancelled",
}
_NONTERMINAL_LIFECYCLE_STATUSES = {"queued", "running"}

# Current-state terminal fields a retry CAS strips when requeueing a
# failed/cancelled job — the fresh queued read model must not expose stale
# failure or cancel evidence from the previous run.
_STALE_TERMINAL_JOB_METADATA_KEYS = frozenset(
    {"error", "warnings", "failure_exception", "failed_at", "finished_at", "cancelled_at"}
)

# Permanent failure codes: retrying cannot change the outcome because the
# defect is in the input type/structure, an orphaned object, or the attempt
# budget itself. Evidence-missing codes (source_missing,
# canonical_markdown_missing) are NOT permanent: re-uploading the same bytes
# rewrites the spool/artifact, and an explicit retry then succeeds.
_NON_RETRYABLE_JOB_ERROR_CODES = frozenset(
    {
        "unsupported_file_type",
        "document_missing",
        "import_payload_invalid",
        "personal_kb_import_attempt_limit_exceeded",
    }
)


def _job_retryable(*, lifecycle_status: str, attempt_count: int, max_attempts: int, error_code: str | None) -> bool:
    """Single retryability authority shared by the read model and the retry
    CAS: failed and cancelled jobs may requeue while attempts remain, unless
    the failure code is permanent."""
    if lifecycle_status not in {"failed", "cancelled"}:
        return False
    if int(attempt_count) >= max(1, int(max_attempts)):
        return False
    return error_code not in _NON_RETRYABLE_JOB_ERROR_CODES


def _job_error_code(job: Any) -> str | None:
    metadata = dict(getattr(job, "job_metadata_json", {}) or {})
    code = str(metadata.get("error") or "").strip()
    if code:
        return code
    message = str(getattr(job, "error_message", None) or "").strip()
    if not message:
        return None
    head = message.split(":", 1)[0].strip()
    return head or None


def _job_lifecycle_view(job: Any) -> PersonalKnowledgeJobSummary:
    status = str(getattr(job, "status", "") or "").lower()
    attempt_count = int(getattr(job, "attempt_count", 0) or 0)
    metadata = dict(getattr(job, "job_metadata_json", {}) or {})
    max_attempts = int(metadata.get("max_attempts") or _DEFAULT_IMPORT_JOB_MAX_ATTEMPTS)
    lifecycle_status = _JOB_LIFECYCLE_BY_RAW_STATUS.get(status, status)
    result_status = _JOB_RESULT_BY_RAW_STATUS.get(status)
    terminal = lifecycle_status not in _NONTERMINAL_LIFECYCLE_STATUSES
    cancelled_at = str(metadata.get("cancelled_at") or "") or None
    error_code = _job_error_code(job)
    return PersonalKnowledgeJobSummary(
        job_id=getattr(job, "id", None),
        document_id=getattr(job, "document_id", None),
        stage=str(getattr(job, "stage", "") or ""),
        status=status,
        artifact_hash=str(getattr(job, "artifact_hash", "") or ""),
        error_message=getattr(job, "error_message", None),
        attempt_count=attempt_count,
        metadata=metadata,
        created_at=getattr(job, "created_at", None),
        updated_at=getattr(job, "updated_at", None),
        terminal=terminal,
        retryable=_job_retryable(
            lifecycle_status=lifecycle_status,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            error_code=error_code,
        ),
        cancellable=lifecycle_status == "queued",
        error_code=error_code,
        max_attempts=max_attempts,
        lifecycle_status=lifecycle_status,
        result_status=result_status,
        cancelled_at=cancelled_at,
    )


@dataclass(frozen=True)
class PersonalKnowledgeGrantSummary:
    grant_id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID
    document_id: uuid.UUID | None
    grantee_type: str
    grantee_id: uuid.UUID
    permission: str
    requester_user_id: uuid.UUID | None
    session_id: str | None
    purpose: str | None
    delegation_id: str | None
    sensitivity_ceiling: str
    binding_key: str
    expires_at: Any
    revoked_at: Any
    revoked_by_user_id: uuid.UUID | None
    active: bool
    metadata: dict[str, Any]
    created_at: Any


@dataclass(frozen=True)
class PersonalKnowledgeGraphEntity:
    entity_id: uuid.UUID
    canonical_name: str
    entity_type: str
    aliases: list[str]
    description: str | None
    confidence: float
    source_refs: list[dict[str, Any]]


@dataclass(frozen=True)
class PersonalKnowledgeGraphLink:
    link_id: uuid.UUID
    from_kind: str
    from_id: uuid.UUID
    to_kind: str
    to_id: uuid.UUID
    relation: str
    confidence: float
    source_refs: list[dict[str, Any]]


@dataclass(frozen=True)
class PersonalKnowledgeGraphAssertion:
    assertion_id: uuid.UUID
    subject_text: str
    predicate: str
    object_text: str
    confidence: float
    status: str
    source_refs: list[dict[str, Any]]


@dataclass(frozen=True)
class PersonalKnowledgeGraphSummary:
    entities: list[PersonalKnowledgeGraphEntity]
    links: list[PersonalKnowledgeGraphLink]
    assertions: list[PersonalKnowledgeGraphAssertion]


class PersonalKnowledgeService:
    """Write person-scope canonical Markdown into the Knowledge Core tables."""

    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        conversion_service: Any | None = None,
        extractor: Any = _DEFAULT_EXTRACTOR,
        media_provider: Any | None = None,
        vector_provider: Any | None = None,
        conversion_timeout_seconds: float | None = None,
    ) -> None:
        self.data_root = Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)
        self.conversion_service = conversion_service
        self.extractor = extractor
        self.media_provider = media_provider
        self.vector_provider = vector_provider
        if conversion_timeout_seconds is None:
            conversion_timeout_seconds = float(get_settings().PERSONAL_KB_CONVERSION_TIMEOUT_SECONDS)
        self.conversion_timeout_seconds = max(0.001, float(conversion_timeout_seconds))

    def _knowledge_extractor(self) -> Any | None:
        if self.extractor is None:
            return None
        if self.extractor is not _DEFAULT_EXTRACTOR:
            return self.extractor
        from app.services.personal_knowledge_extractor import PersonalKnowledgeLLMExtractor

        return PersonalKnowledgeLLMExtractor()

    def _media_transcription_provider(self) -> Any | None:
        return self.media_provider

    def _vector_index_provider(self) -> Any | None:
        return self.vector_provider

    async def _extract_segment_with_tenant_guard(
        self,
        *,
        extractor: Any,
        segment: Any,
        document: Any,
        source_ref: dict[str, Any],
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        sensitivity: str,
    ) -> Any:
        async with _extract_semaphore_for_tenant(tenant_id):
            return await extractor.extract_segment(
                segment=segment,
                document=document,
                source_ref=source_ref,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                sensitivity=sensitivity,
            )

    def _document_summary(
        self,
        *,
        owner_user_id: uuid.UUID,
        document: Any,
        segment_count: int,
    ) -> PersonalKnowledgeDocumentSummary:
        document_id = document.id
        return PersonalKnowledgeDocumentSummary(
            document_id=document_id,
            title=str(document.title or "Untitled knowledge document"),
            source_kind=str(document.source_kind or "unknown"),
            source_uri=document.source_uri,
            source_sha256=str(document.source_sha256 or ""),
            source_ref=f"kb://person/{owner_user_id}/documents/{document_id}",
            canonical_md_path=str(document.canonical_md_path or ""),
            status=str(document.status or "unknown"),
            sensitivity=str(document.sensitivity or "internal"),
            agent_searchable=bool(document.agent_searchable),
            segment_count=int(segment_count or 0),
            created_at=document.created_at,
            updated_at=document.updated_at,
            metadata=dict(document.doc_metadata_json or {}),
        )

    async def list_personal_documents(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        principal: PersonalKnowledgePrincipal,
        limit: int = 50,
    ) -> list[PersonalKnowledgeDocumentSummary]:
        statement = build_personal_knowledge_document_list_statement(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            principal=principal,
            limit=limit,
        )
        rows = (await session.execute(statement)).all()
        return [
            self._document_summary(owner_user_id=owner_user_id, document=row[0], segment_count=row[1]) for row in rows
        ]

    async def get_personal_document(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        principal: PersonalKnowledgePrincipal,
    ) -> PersonalKnowledgeDocumentDetail | None:
        statement = build_personal_knowledge_document_list_statement(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            principal=principal,
            limit=1,
            document_id=document_id,
        )
        rows = (await session.execute(statement)).all()
        if not rows:
            return None
        document, segment_count = rows[0][0], rows[0][1]
        try:
            if canonicalize_sensitivity(document.sensitivity) == SensitivityLevel.PL4_CREDENTIAL:
                return None
        except ValueError:
            return None
        segment_rows = (
            await session.execute(
                select(KnowledgeSegment)
                .where(
                    KnowledgeSegment.tenant_id == tenant_id,
                    KnowledgeSegment.document_id == document.id,
                    KnowledgeSegment.scope_type == "person",
                    KnowledgeSegment.scope_id == owner_user_id,
                )
                .order_by(KnowledgeSegment.position.asc())
            )
        ).all()
        segments: list[PersonalKnowledgeDocumentSegment] = []
        for row in segment_rows:
            try:
                segment = row[0]
            except (TypeError, KeyError):
                segment = row
            segments.append(
                PersonalKnowledgeDocumentSegment(
                    segment_id=segment.id,
                    position=int(segment.position),
                    heading_path=list(segment.heading_path_json or []),
                    content=str(segment.content or ""),
                    token_count=int(segment.token_count or 0),
                )
            )
        summary = self._document_summary(owner_user_id=owner_user_id, document=document, segment_count=segment_count)
        return PersonalKnowledgeDocumentDetail(**summary.__dict__, segments=segments)

    async def get_personal_document_with_authority(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        principal: PersonalKnowledgePrincipal,
    ) -> PersonalKnowledgeDocumentReadResult:
        """Fresh-check document authority before any title or segment content is loaded."""

        metadata_rows = (
            await session.execute(
                select(
                    KnowledgeDocument.id,
                    KnowledgeDocument.sensitivity,
                    KnowledgeDocument.agent_searchable,
                    KnowledgeDocument.doc_metadata_json,
                ).where(
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.scope_type == "person",
                    KnowledgeDocument.scope_id == owner_user_id,
                    KnowledgeDocument.id == document_id,
                    personal_knowledge_consumable_status_predicate(principal=principal),
                )
            )
        ).all()
        if not metadata_rows:
            scope_decision = await resolve_personal_knowledge_permission(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                principal=principal,
                action="read",
            )
            return PersonalKnowledgeDocumentReadResult(
                status="empty" if scope_decision.allowed else "denied",
                document=None,
                credential_reference=None,
                authority=replace(
                    scope_decision,
                    document_id=document_id,
                    deny_reason_code=None if scope_decision.allowed else scope_decision.deny_reason_code,
                ),
                warnings=["document_not_found"] if scope_decision.allowed else [],
            )

        row = metadata_rows[0]
        sensitivity = str(row[1] or "")
        agent_searchable = bool(row[2])
        metadata = dict(row[3] or {})
        decision = await resolve_personal_knowledge_permission(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            principal=principal,
            action="read",
            document_id=document_id,
            document_sensitivity=sensitivity,
        )
        if not decision.allowed:
            return PersonalKnowledgeDocumentReadResult(
                status="denied",
                document=None,
                credential_reference=None,
                authority=decision,
            )
        if isinstance(principal, AgentRuntimePrincipal) and not agent_searchable:
            return PersonalKnowledgeDocumentReadResult(
                status="denied",
                document=None,
                credential_reference=None,
                authority=replace(
                    decision,
                    allowed=False,
                    authority_source="none",
                    deny_reason_code="agent_searchable_disabled",
                ),
            )
        try:
            canonical_sensitivity = canonicalize_sensitivity(sensitivity)
        except ValueError:
            return PersonalKnowledgeDocumentReadResult(
                status="denied",
                document=None,
                credential_reference=None,
                authority=replace(
                    decision,
                    allowed=False,
                    authority_source="none",
                    deny_reason_code="document_sensitivity_invalid",
                ),
            )
        if canonical_sensitivity == SensitivityLevel.PL4_CREDENTIAL:
            credential_reference = _credential_reference_from_metadata(metadata)
            return PersonalKnowledgeDocumentReadResult(
                status="ok" if credential_reference else "unavailable",
                document=None,
                credential_reference=credential_reference,
                authority=replace(decision, credential_reference_only=True),
                warnings=[] if credential_reference else ["credential_reference_unavailable"],
            )

        document = await self.get_personal_document(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            document_id=document_id,
            principal=principal,
        )
        if document is None:
            return PersonalKnowledgeDocumentReadResult(
                status="unavailable",
                document=None,
                credential_reference=None,
                authority=replace(decision, retryable=True),
                warnings=["authority_or_document_changed_during_read"],
            )
        return PersonalKnowledgeDocumentReadResult(
            status="ok",
            document=document,
            credential_reference=None,
            authority=decision,
        )

    def _source_preview_from_metadata(self, metadata: dict[str, Any]) -> PersonalKnowledgeSourcePreview | None:
        source_path = str(metadata.get("queued_source_path") or "").strip()
        if not source_path:
            return None
        filename = _safe_filename(str(metadata.get("source_filename") or Path(source_path).name))
        mime_type = str(metadata.get("source_mime_type") or mimetypes.guess_type(filename)[0] or "").strip().lower()
        media_kind = str(metadata.get("media_kind") or "").strip().lower()
        if not (mime_type.startswith("image/") or media_kind == "image"):
            return None

        root = self.data_root.resolve()
        candidate = (root / source_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return PersonalKnowledgeSourcePreview(
            filename=filename,
            mime_type=mime_type or "application/octet-stream",
            content=candidate.read_bytes(),
        )

    async def get_personal_document_source_preview(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        principal: PersonalKnowledgePrincipal,
    ) -> PersonalKnowledgeSourcePreview | None:
        statement = build_personal_knowledge_document_list_statement(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            principal=principal,
            limit=1,
            document_id=document_id,
        )
        rows = (await session.execute(statement)).all()
        if not rows:
            return None
        document = rows[0][0]
        try:
            if canonicalize_sensitivity(document.sensitivity) == SensitivityLevel.PL4_CREDENTIAL:
                return None
        except ValueError:
            return None
        preview = self._source_preview_from_metadata(dict(getattr(document, "doc_metadata_json", {}) or {}))
        if preview is not None:
            return preview

        job_rows = (
            await session.execute(
                select(KnowledgeIndexJob)
                .where(
                    KnowledgeIndexJob.tenant_id == tenant_id,
                    KnowledgeIndexJob.scope_type == "person",
                    KnowledgeIndexJob.scope_id == owner_user_id,
                    KnowledgeIndexJob.document_id == document_id,
                )
                .order_by(KnowledgeIndexJob.updated_at.desc(), KnowledgeIndexJob.created_at.desc())
                .limit(5)
            )
        ).all()
        for row in job_rows:
            try:
                job = row[0]
            except (TypeError, KeyError):
                job = row
            preview = self._source_preview_from_metadata(dict(getattr(job, "job_metadata_json", {}) or {}))
            if preview is not None:
                return preview
        return None

    async def _upsert_index_job(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        artifact_hash: str,
        source_kind: str,
        warnings: list[str],
        stage: str,
        status: str,
        error_message: str | None = None,
        channels: list[str] | None = None,
        attempt_increment: int = 1,
        managed_job_id: uuid.UUID | None = None,
    ) -> Any:
        if managed_job_id is not None:
            # The claiming worker owns the job row lifecycle: ingest stages
            # document/segment writes only and must never read, mutate, or
            # flush the managed KnowledgeIndexJob row — the fenced terminal
            # CAS is the sole job-row writer after phase 1.
            return SimpleNamespace(id=managed_job_id, job_metadata_json={})
        result = await session.execute(
            select(KnowledgeIndexJob).where(
                KnowledgeIndexJob.tenant_id == tenant_id,
                KnowledgeIndexJob.document_id == document_id,
                KnowledgeIndexJob.artifact_hash == artifact_hash,
            )
        )
        job = result.scalar_one_or_none()
        # The worker claim owns attempt accounting exactly once: claimed runs
        # pass attempt_increment=0 so the same job is never counted twice;
        # direct ingest without a pre-claimed job keeps its own increment.
        increment = max(0, int(attempt_increment or 0))
        if not isinstance(job, KnowledgeIndexJob):
            # A brand-new job row has no pre-claimed worker: this upsert is its
            # only attempt record, so it always counts at least one execution.
            job = KnowledgeIndexJob(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                document_id=document_id,
                scope_type="person",
                scope_id=owner_user_id,
                artifact_hash=artifact_hash,
                attempt_count=max(1, increment),
            )
            session.add(job)
        else:
            job.attempt_count = int(job.attempt_count or 0) + increment
        job.stage = stage
        job.status = status
        job.error_message = error_message
        job.job_metadata_json = {
            **(job.job_metadata_json or {}),
            "channels": channels or ["tsvector", "segments"],
            "source_kind": source_kind,
            "warnings": list(warnings),
        }
        await session.flush()
        return job

    async def _clear_document_graph_projection(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> None:
        document_ref = [{"document_id": str(document_id)}]
        await session.execute(
            delete(KnowledgeAssertion).where(
                KnowledgeAssertion.tenant_id == tenant_id,
                KnowledgeAssertion.scope_type == "person",
                KnowledgeAssertion.scope_id == owner_user_id,
                KnowledgeAssertion.source_document_id == document_id,
            )
        )
        await session.execute(
            delete(KnowledgeLink).where(
                KnowledgeLink.tenant_id == tenant_id,
                KnowledgeLink.scope_type == "person",
                KnowledgeLink.scope_id == owner_user_id,
                KnowledgeLink.source_refs_json.contains(document_ref),
            )
        )

    async def _upsert_entity(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        canonical_name: str,
        entity_type: str,
        aliases: list[str],
        description: str | None,
        confidence: float,
        source_ref: dict[str, Any],
    ) -> KnowledgeEntity | None:
        clean_name = _clean_graph_text(canonical_name)
        clean_type = _clean_graph_text(entity_type, max_len=80) or "freeform"
        if not clean_name:
            return None
        result = await session.execute(
            select(KnowledgeEntity).where(
                KnowledgeEntity.tenant_id == tenant_id,
                KnowledgeEntity.scope_type == "person",
                KnowledgeEntity.scope_id == owner_user_id,
                KnowledgeEntity.entity_type == clean_type,
                KnowledgeEntity.canonical_name == clean_name,
            )
        )
        entity = result.scalar_one_or_none()
        if not isinstance(entity, KnowledgeEntity):
            entity = KnowledgeEntity(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                scope_type="person",
                scope_id=owner_user_id,
                canonical_name=clean_name,
                entity_type=clean_type,
                aliases_json=[],
                source_refs_json=[],
            )
            session.add(entity)
        entity.aliases_json = _merge_unique_strings(entity.aliases_json, aliases)
        entity.description = description or entity.description
        entity.confidence = max(_coerce_confidence(entity.confidence), _coerce_confidence(confidence))
        entity.source_refs_json = _merge_source_refs(entity.source_refs_json, [source_ref])
        await session.flush()

        for alias in entity.aliases_json:
            alias_result = await session.execute(
                select(KnowledgeEntity).where(
                    KnowledgeEntity.tenant_id == tenant_id,
                    KnowledgeEntity.scope_type == "person",
                    KnowledgeEntity.scope_id == owner_user_id,
                    KnowledgeEntity.entity_type == clean_type,
                    KnowledgeEntity.canonical_name == alias,
                    KnowledgeEntity.id != entity.id,
                )
            )
            alias_entity = alias_result.scalar_one_or_none()
            if isinstance(alias_entity, KnowledgeEntity):
                alias_entity.merged_into_entity_id = entity.id
                alias_entity.source_refs_json = _merge_source_refs(alias_entity.source_refs_json, [source_ref])
        return entity

    async def _upsert_assertion(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        subject_text: str,
        predicate: str,
        object_text: str,
        confidence: float,
        source_ref: dict[str, Any],
        entity_by_name: dict[str, KnowledgeEntity],
    ) -> KnowledgeAssertion | None:
        subject = _clean_graph_text(subject_text)
        pred = _clean_graph_text(predicate, max_len=120)
        obj = _clean_graph_text(object_text, max_len=None)
        if not subject or not pred or not obj:
            return None
        result = await session.execute(
            select(KnowledgeAssertion).where(
                KnowledgeAssertion.tenant_id == tenant_id,
                KnowledgeAssertion.scope_type == "person",
                KnowledgeAssertion.scope_id == owner_user_id,
                KnowledgeAssertion.subject_text == subject,
                KnowledgeAssertion.predicate == pred,
                KnowledgeAssertion.object_text == obj,
            )
        )
        assertion = result.scalar_one_or_none()
        subject_entity = entity_by_name.get(subject.lower())
        object_entity = entity_by_name.get(obj.lower())
        if not isinstance(assertion, KnowledgeAssertion):
            assertion = KnowledgeAssertion(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                scope_type="person",
                scope_id=owner_user_id,
                source_document_id=document_id,
                subject_text=subject,
                predicate=pred,
                object_text=obj,
                source_refs_json=[],
            )
            session.add(assertion)
        assertion.source_document_id = document_id
        assertion.subject_entity_id = subject_entity.id if subject_entity else assertion.subject_entity_id
        assertion.object_entity_id = object_entity.id if object_entity else assertion.object_entity_id
        assertion.confidence = max(_coerce_confidence(assertion.confidence), _coerce_confidence(confidence))
        assertion.status = "active"
        assertion.source_refs_json = _merge_source_refs(assertion.source_refs_json, [source_ref])
        return assertion

    async def _upsert_link(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        relation: str,
        confidence: float,
        source_ref: dict[str, Any],
        from_entity: KnowledgeEntity,
        to_entity: KnowledgeEntity,
    ) -> KnowledgeLink | None:
        clean_relation = _clean_graph_text(relation, max_len=80)
        if not clean_relation:
            return None
        result = await session.execute(
            select(KnowledgeLink).where(
                KnowledgeLink.tenant_id == tenant_id,
                KnowledgeLink.scope_type == "person",
                KnowledgeLink.scope_id == owner_user_id,
                KnowledgeLink.from_kind == "entity",
                KnowledgeLink.from_id == from_entity.id,
                KnowledgeLink.to_kind == "entity",
                KnowledgeLink.to_id == to_entity.id,
                KnowledgeLink.relation == clean_relation,
            )
        )
        link = result.scalar_one_or_none()
        if not isinstance(link, KnowledgeLink):
            link = KnowledgeLink(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                scope_type="person",
                scope_id=owner_user_id,
                from_kind="entity",
                from_id=from_entity.id,
                to_kind="entity",
                to_id=to_entity.id,
                relation=clean_relation,
                source_refs_json=[],
            )
            session.add(link)
        link.confidence = max(_coerce_confidence(link.confidence), _coerce_confidence(confidence))
        link.source_refs_json = _merge_source_refs(link.source_refs_json, [source_ref])
        return link

    async def _extract_and_write_graph(
        self,
        session: Any,
        *,
        extractor: Any,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document: KnowledgeDocument,
        segments: list[KnowledgeSegment],
        sensitivity: str,
        extraction_usage: dict[str, Any] | None = None,
    ) -> list[str]:
        warnings: list[str] = []
        entity_by_name: dict[str, KnowledgeEntity] = {}
        for segment in segments:
            source_ref = {
                "document_id": str(document.id),
                "segment_id": str(segment.id),
                "seg_hash": str(segment.segment_hash),
                "heading_path": list(segment.heading_path_json or []),
                "position": int(segment.position),
            }
            extraction = await self._extract_segment_with_tenant_guard(
                extractor=extractor,
                segment=segment,
                document=document,
                source_ref=source_ref,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                sensitivity=sensitivity,
            )
            if extraction_usage is not None:
                _record_extraction_usage(extraction_usage, extraction)
            warnings.extend([str(warning) for warning in getattr(extraction, "warnings", ()) or []])

            for extracted_entity in getattr(extraction, "entities", ()) or []:
                entity = await self._upsert_entity(
                    session,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    canonical_name=getattr(extracted_entity, "canonical_name", ""),
                    entity_type=getattr(extracted_entity, "entity_type", "freeform"),
                    aliases=list(getattr(extracted_entity, "aliases", ()) or []),
                    description=getattr(extracted_entity, "description", None),
                    confidence=getattr(extracted_entity, "confidence", 1.0),
                    source_ref=source_ref,
                )
                if entity is None:
                    continue
                entity_by_name[entity.canonical_name.lower()] = entity
                for alias in entity.aliases_json:
                    entity_by_name[alias.lower()] = entity

            for extracted_assertion in getattr(extraction, "assertions", ()) or []:
                await self._upsert_assertion(
                    session,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    document_id=document.id,
                    subject_text=getattr(extracted_assertion, "subject_text", ""),
                    predicate=getattr(extracted_assertion, "predicate", ""),
                    object_text=getattr(extracted_assertion, "object_text", ""),
                    confidence=getattr(extracted_assertion, "confidence", 1.0),
                    source_ref=source_ref,
                    entity_by_name=entity_by_name,
                )

            for extracted_link in getattr(extraction, "links", ()) or []:
                from_entity = entity_by_name.get(_clean_graph_text(getattr(extracted_link, "from_name", "")).lower())
                if from_entity is None:
                    from_entity = await self._upsert_entity(
                        session,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        canonical_name=getattr(extracted_link, "from_name", ""),
                        entity_type=getattr(extracted_link, "from_type", "freeform"),
                        aliases=[],
                        description=None,
                        confidence=getattr(extracted_link, "confidence", 1.0),
                        source_ref=source_ref,
                    )
                to_entity = entity_by_name.get(_clean_graph_text(getattr(extracted_link, "to_name", "")).lower())
                if to_entity is None:
                    to_entity = await self._upsert_entity(
                        session,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        canonical_name=getattr(extracted_link, "to_name", ""),
                        entity_type=getattr(extracted_link, "to_type", "freeform"),
                        aliases=[],
                        description=None,
                        confidence=getattr(extracted_link, "confidence", 1.0),
                        source_ref=source_ref,
                    )
                if from_entity is None or to_entity is None:
                    continue
                entity_by_name[from_entity.canonical_name.lower()] = from_entity
                entity_by_name[to_entity.canonical_name.lower()] = to_entity
                await self._upsert_link(
                    session,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    relation=getattr(extracted_link, "relation", ""),
                    confidence=getattr(extracted_link, "confidence", 1.0),
                    source_ref=source_ref,
                    from_entity=from_entity,
                    to_entity=to_entity,
                )
        await session.flush()
        return warnings

    async def _queue_import_job(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        title: str,
        source_kind: str,
        source_uri: str | None,
        source_sha256: str,
        artifact_hash: str,
        canonical_md_path: str,
        canonical_md_sha256: str | None,
        created_by_user_id: uuid.UUID | None,
        agent_searchable: bool,
        sensitivity: str,
        metadata: dict[str, Any],
    ) -> PersonalKnowledgeIngestResult:
        canonical_sensitivity = canonicalize_sensitivity(sensitivity).value
        clean_source_sha256 = _validate_source_sha256(source_sha256)
        clean_source_kind = _clean_title(source_kind).lower().replace(" ", "_")
        clean_title = _clean_title(title)
        existing_result = await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.scope_type == "person",
                KnowledgeDocument.scope_id == owner_user_id,
                KnowledgeDocument.source_sha256 == clean_source_sha256,
            )
        )
        document = existing_result.scalar_one_or_none()
        if document is not None:
            # Idempotent duplicate upload: identical source bytes for the same
            # owner return the existing document/job untouched — ready stays
            # ready, archived stays archived, attempts stay stable. Only the
            # explicit Retry / Restore / Rebuild actions own reprocessing, so a
            # duplicate can never reset a terminal state or resurrect a job at
            # the attempt ceiling back into a permanent queued loop.
            job_result = await session.execute(
                select(KnowledgeIndexJob).where(
                    KnowledgeIndexJob.tenant_id == tenant_id,
                    KnowledgeIndexJob.document_id == document.id,
                    KnowledgeIndexJob.artifact_hash == artifact_hash,
                )
            )
            existing_job = job_result.scalar_one_or_none()
            # Truthful dedupe: when the persisted document has no matching job
            # row (legacy data), the result carries job_id=None — a fabricated
            # job id would be nonexistent evidence.
            existing_job_id = getattr(existing_job, "id", None)
            existing_status = str(getattr(document, "status", "") or "queued")
            await session.flush()
            return PersonalKnowledgeIngestResult(
                document_id=document.id,
                job_id=existing_job_id,
                source_sha256=clean_source_sha256,
                artifact_hash=str(getattr(document, "artifact_hash", "") or artifact_hash),
                canonical_md_path=str(getattr(document, "canonical_md_path", "") or ""),
                segment_count=int(dict(getattr(document, "doc_metadata_json", {}) or {}).get("segment_count") or 0),
                status=existing_status,
                warnings=[],
            )
        document_metadata = {
            **metadata,
            "sensitivity": canonical_sensitivity,
            "queued_import_kind": metadata.get("queued_import_kind"),
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        document = KnowledgeDocument(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            scope_type="person",
            scope_id=owner_user_id,
            owner_user_id=owner_user_id,
            source_kind=clean_source_kind,
            source_uri=source_uri,
            source_sha256=clean_source_sha256,
            artifact_hash=artifact_hash,
            title=clean_title,
            status="queued",
            sensitivity=canonical_sensitivity,
            agent_searchable=agent_searchable,
            canonical_md_path=canonical_md_path,
            canonical_md_sha256=canonical_md_sha256,
            doc_metadata_json=document_metadata,
            created_by_user_id=created_by_user_id,
        )
        session.add(document)
        await session.flush()

        job_result = await session.execute(
            select(KnowledgeIndexJob).where(
                KnowledgeIndexJob.tenant_id == tenant_id,
                KnowledgeIndexJob.document_id == document.id,
                KnowledgeIndexJob.artifact_hash == artifact_hash,
            )
        )
        job = job_result.scalar_one_or_none()
        job_metadata = {
            **(getattr(job, "job_metadata_json", {}) or {}),
            **metadata,
            "sensitivity": canonical_sensitivity,
            "source_kind": clean_source_kind,
            "source_sha256": clean_source_sha256,
            "warnings": [],
        }
        if job is None:
            job = KnowledgeIndexJob(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                document_id=document.id,
                scope_type="person",
                scope_id=owner_user_id,
                artifact_hash=artifact_hash,
                stage="queued",
                status="queued",
                error_message=None,
                attempt_count=0,
                job_metadata_json=job_metadata,
            )
            session.add(job)
        else:
            job.stage = "queued"
            job.status = "queued"
            job.error_message = None
            job.job_metadata_json = job_metadata
        await session.flush()

        return PersonalKnowledgeIngestResult(
            document_id=document.id,
            job_id=job.id,
            source_sha256=clean_source_sha256,
            artifact_hash=artifact_hash,
            canonical_md_path=canonical_md_path,
            segment_count=0,
            status="queued",
            warnings=[],
        )

    async def queue_markdown_import(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        title: str,
        markdown: str,
        source_kind: str,
        source_uri: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        agent_searchable: bool = True,
        sensitivity: str = "internal",
        doc_metadata: dict[str, Any] | None = None,
        source_sha256: str | None = None,
    ) -> PersonalKnowledgeIngestResult:
        canonical_sensitivity = canonicalize_sensitivity(sensitivity).value
        canonical_md = _normalize_markdown(markdown)
        if not canonical_md:
            raise ValueError("markdown must not be empty")
        clean_source_kind = _clean_title(source_kind).lower().replace(" ", "_")
        source_payload = "\n".join([clean_source_kind, source_uri or "", canonical_md])
        source_hash = _validate_source_sha256(source_sha256) if source_sha256 else _sha256(source_payload)
        artifact_hash = _sha256(canonical_md)
        artifact_path = personal_knowledge_artifact_path(self.data_root, owner_user_id, source_hash)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(canonical_md, encoding="utf-8")
        canonical_md_path = artifact_path.relative_to(self.data_root).as_posix()
        metadata = {
            "queued_import_kind": "markdown",
            "queued_markdown_path": canonical_md_path,
            "title": _clean_title(title),
            "source_kind": clean_source_kind,
            "source_uri": source_uri,
            "created_by_user_id": str(created_by_user_id) if created_by_user_id else None,
            "agent_searchable": bool(agent_searchable),
            "sensitivity": canonical_sensitivity,
            "doc_metadata": dict(doc_metadata or {}),
        }
        return await self._queue_import_job(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            title=title,
            source_kind=clean_source_kind,
            source_uri=source_uri,
            source_sha256=source_hash,
            artifact_hash=artifact_hash,
            canonical_md_path=canonical_md_path,
            canonical_md_sha256=artifact_hash,
            created_by_user_id=created_by_user_id,
            agent_searchable=agent_searchable,
            sensitivity=canonical_sensitivity,
            metadata=metadata,
        )

    async def queue_source_bytes_import(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        filename: str,
        data: bytes,
        title: str | None = None,
        source_kind: str = "upload",
        source_uri: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        agent_searchable: bool = True,
        sensitivity: str = "internal",
        source_mime_type: str | None = None,
        doc_metadata: dict[str, Any] | None = None,
    ) -> PersonalKnowledgeIngestResult:
        canonical_sensitivity = canonicalize_sensitivity(sensitivity).value
        safe_name = _safe_filename(filename)
        source_hash = _sha256_bytes(data)
        spool_path = personal_knowledge_import_spool_path(self.data_root, owner_user_id, source_hash, safe_name)
        spool_path.parent.mkdir(parents=True, exist_ok=True)
        spool_path.write_bytes(data)
        clean_source_kind = _clean_title(source_kind).lower().replace(" ", "_")
        clean_title = title_from_filename_or_uri(safe_name, source_uri, title)
        metadata = {
            "queued_import_kind": "source_bytes",
            "queued_source_path": spool_path.relative_to(self.data_root).as_posix(),
            "source_filename": safe_name,
            "title": clean_title,
            "source_kind": clean_source_kind,
            "source_uri": source_uri,
            "source_mime_type": source_mime_type or mimetypes.guess_type(safe_name)[0] or "",
            "created_by_user_id": str(created_by_user_id) if created_by_user_id else None,
            "agent_searchable": bool(agent_searchable),
            "sensitivity": canonical_sensitivity,
            "doc_metadata": dict(doc_metadata or {}),
        }
        return await self._queue_import_job(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            title=clean_title,
            source_kind=clean_source_kind,
            source_uri=source_uri,
            source_sha256=source_hash,
            artifact_hash=source_hash,
            canonical_md_path="",
            canonical_md_sha256=None,
            created_by_user_id=created_by_user_id,
            agent_searchable=agent_searchable,
            sensitivity=canonical_sensitivity,
            metadata=metadata,
        )

    async def queue_url_import(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        url: str,
        title: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        agent_searchable: bool = True,
        sensitivity: str = "internal",
    ) -> PersonalKnowledgeIngestResult:
        canonical_sensitivity = canonicalize_sensitivity(sensitivity).value
        clean_url = str(url or "").strip()
        parsed = urlparse(clean_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute http(s) URL")
        filename = Path(parsed.path).name or "imported-url.html"
        clean_title = title_from_filename_or_uri(filename, clean_url, title)
        source_hash = _sha256("\n".join(["url", clean_url]))
        metadata = {
            "queued_import_kind": "url",
            "title": clean_title,
            "source_kind": "url",
            "source_uri": clean_url,
            "created_by_user_id": str(created_by_user_id) if created_by_user_id else None,
            "agent_searchable": bool(agent_searchable),
            "sensitivity": canonical_sensitivity,
            "doc_metadata": {},
        }
        return await self._queue_import_job(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            title=clean_title,
            source_kind="url",
            source_uri=clean_url,
            source_sha256=source_hash,
            artifact_hash=source_hash,
            canonical_md_path="",
            canonical_md_sha256=None,
            created_by_user_id=created_by_user_id,
            agent_searchable=agent_searchable,
            sensitivity=canonical_sensitivity,
            metadata=metadata,
        )

    async def ingest_markdown(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        title: str,
        markdown: str,
        source_kind: str,
        source_uri: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        agent_searchable: bool = True,
        sensitivity: str = "internal",
        source_sha256: str | None = None,
        doc_metadata: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        force_reindex: bool = False,
        attempt_increment: int = 1,
        managed_job_id: uuid.UUID | None = None,
    ) -> PersonalKnowledgeIngestResult:
        canonical_sensitivity = canonicalize_sensitivity(sensitivity).value
        canonical_md = _normalize_markdown(markdown)
        if not canonical_md:
            raise ValueError("markdown must not be empty")

        clean_title = _clean_title(title)
        clean_source_kind = _clean_title(source_kind).lower().replace(" ", "_")
        source_payload = "\n".join([clean_source_kind, source_uri or "", canonical_md])
        clean_source_sha256 = str(source_sha256 or _sha256(source_payload)).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", clean_source_sha256):
            raise ValueError("source_sha256 must be a 64-character lowercase hex digest")
        artifact_hash = _sha256(canonical_md)
        artifact_path = personal_knowledge_artifact_path(self.data_root, owner_user_id, clean_source_sha256)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(canonical_md, encoding="utf-8")
        canonical_md_path = artifact_path.relative_to(self.data_root).as_posix()

        existing_result = await session.execute(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.scope_type == "person",
                KnowledgeDocument.scope_id == owner_user_id,
                KnowledgeDocument.source_sha256 == clean_source_sha256,
            )
            .with_for_update()
            # The locked read must see the committed row, not a stale
            # identity-map snapshot (e.g. a rebuild loaded the document in
            # this session before the user archived it).
            .execution_options(populate_existing=True)
        )
        document = existing_result.scalar_one_or_none()
        previous_artifact_hash = document.canonical_md_sha256 if document is not None else None
        # Archive ownership: a user archive committed before this ingest owns
        # the consumable status. The row lock above serializes the two writers;
        # an archived document keeps "archived" while content production still
        # completes, and the final consumable status is recorded as the
        # restore target instead of flipping the document back.
        document_archived = document is not None and str(document.status or "") == "archived"
        if document is None:
            document = KnowledgeDocument(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                scope_type="person",
                scope_id=owner_user_id,
                owner_user_id=owner_user_id,
                source_kind=clean_source_kind,
                source_uri=source_uri,
                source_sha256=clean_source_sha256,
                artifact_hash=artifact_hash,
                title=clean_title,
                status="ready",
                sensitivity=canonical_sensitivity,
                agent_searchable=agent_searchable,
                canonical_md_path=canonical_md_path,
                canonical_md_sha256=artifact_hash,
                doc_metadata_json={"ingest_format": "canonical_markdown", **(doc_metadata or {})},
                created_by_user_id=created_by_user_id,
            )
            session.add(document)
        else:
            document.source_kind = clean_source_kind
            document.source_uri = source_uri
            document.artifact_hash = artifact_hash
            document.title = clean_title
            if not document_archived:
                document.status = "ready"
            document.sensitivity = canonical_sensitivity
            document.agent_searchable = agent_searchable
            document.canonical_md_path = canonical_md_path
            document.canonical_md_sha256 = artifact_hash
            document.doc_metadata_json = {
                **(document.doc_metadata_json or {}),
                "ingest_format": "canonical_markdown",
                **(doc_metadata or {}),
            }
            document.created_by_user_id = created_by_user_id or document.created_by_user_id
        await session.flush()

        await session.execute(delete(KnowledgeSegment).where(KnowledgeSegment.document_id == document.id))

        segment_drafts = segment_markdown(canonical_md)
        segment_objects: list[KnowledgeSegment] = []
        for draft in segment_drafts:
            segment = KnowledgeSegment(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                document_id=document.id,
                scope_type="person",
                scope_id=owner_user_id,
                position=draft.position,
                segment_hash=draft.segment_hash,
                heading_path_json=draft.heading_path,
                content=draft.content,
                token_count=draft.token_count,
                segment_metadata_json={},
            )
            session.add(segment)
            segment_objects.append(segment)
        await session.flush()

        job_id: uuid.UUID | None = None
        job: KnowledgeIndexJob | None = None
        all_warnings = list(warnings or [])
        final_status = "ready"
        final_stage = "indexed"
        final_error: str | None = None
        channels = ["tsvector", "segments"]
        extraction_usage_summary = _new_extraction_usage_summary()
        if previous_artifact_hash != artifact_hash or force_reindex:
            job = await self._upsert_index_job(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                document_id=document.id,
                artifact_hash=artifact_hash,
                source_kind=clean_source_kind,
                warnings=all_warnings,
                stage="segmenting",
                status="running",
                channels=channels,
                attempt_increment=attempt_increment,
                managed_job_id=managed_job_id,
            )
            job_id = job.id

        extractor = self._knowledge_extractor()
        if extractor is not None:
            if is_sensitive_extraction_blocked(canonical_sensitivity):
                final_status = "degraded"
                final_stage = "extracting"
                final_error = "knowledge_extraction_skipped_sensitive"
                all_warnings.append(final_error)
            else:
                try:
                    await self._clear_document_graph_projection(
                        session,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        document_id=document.id,
                    )
                    extraction_warnings = await self._extract_and_write_graph(
                        session,
                        extractor=extractor,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        document=document,
                        segments=segment_objects,
                        sensitivity=canonical_sensitivity,
                        extraction_usage=extraction_usage_summary,
                    )
                    all_warnings.extend(extraction_warnings)
                    channels = ["tsvector", "segments", "graph"]
                except Exception as exc:
                    final_status = "degraded"
                    final_stage = "extracting"
                    final_error = f"knowledge_extraction_failed:{exc}"
                    all_warnings.append(final_error)

        optional_vector_state: dict[str, Any] = {
            "enabled": False,
            "status": "disabled",
            "reason": "provider_unconfigured",
        }
        vector_provider = self._vector_index_provider()
        if vector_provider is not None:
            provider_name = vector_provider.__class__.__name__
            try:
                vector_segments = [
                    {
                        "segment_id": str(segment.id),
                        "document_id": str(document.id),
                        "position": int(segment.position),
                        "heading_path": list(segment.heading_path_json or []),
                        "content": str(segment.content or ""),
                        "index_text": "\n".join(
                            part
                            for part in [
                                clean_title,
                                " / ".join(str(item) for item in list(segment.heading_path_json or [])),
                                str(segment.content or ""),
                            ]
                            if part
                        ),
                        "token_count": int(segment.token_count or 0),
                        "segment_hash": str(segment.segment_hash or ""),
                    }
                    for segment in segment_objects
                ]
                call = vector_provider.index_personal_segments(
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    document_id=document.id,
                    source_sha256=clean_source_sha256,
                    artifact_hash=artifact_hash,
                    title=clean_title,
                    segments=vector_segments,
                )
                vector_result = await call if inspect.isawaitable(call) else call
                optional_vector_state = {
                    "enabled": True,
                    "status": "ready",
                    "provider": provider_name,
                    "indexed_segments": len(vector_segments),
                    "result": dict(vector_result or {}) if isinstance(vector_result, dict) else {},
                }
                if "vector" not in channels:
                    channels = [*channels, "vector"]
            except Exception as exc:
                vector_warning = f"optional_vector_index_failed:{exc}"
                all_warnings.append(vector_warning)
                optional_vector_state = {
                    "enabled": True,
                    "status": "failed",
                    "provider": provider_name,
                    "error": vector_warning,
                }

        extraction_usage_state = _finalize_extraction_usage(extraction_usage_summary)
        if not document_archived:
            document.status = final_status
        document.doc_metadata_json = {
            **(document.doc_metadata_json or {}),
            "ingest_format": "canonical_markdown",
            "warnings": all_warnings,
            "optional_vector": optional_vector_state,
            **(doc_metadata or {}),
        }
        if document_archived:
            # The user's archive owns the consumable status; the worker only
            # records its real final consumable state as the restore target.
            # Written after the caller-metadata merge so a stale lifecycle
            # control field in doc_metadata can never win over the platform.
            document.doc_metadata_json["archived_from_status"] = final_status
        if extraction_usage_state is not None:
            document.doc_metadata_json["extraction_usage"] = extraction_usage_state
        if job is not None:
            job.stage = final_stage
            job.status = final_status
            job.error_message = final_error
            job.job_metadata_json = {
                **(job.job_metadata_json or {}),
                "channels": channels,
                "source_kind": clean_source_kind,
                "warnings": all_warnings,
                "optional_vector": optional_vector_state,
            }
            if extraction_usage_state is not None:
                job.job_metadata_json["extraction_usage"] = extraction_usage_state

        await session.execute(
            update(KnowledgeSegment)
            .where(KnowledgeSegment.document_id == document.id)
            .values(tsv=func.to_tsvector("simple", KnowledgeSegment.content))
        )
        await session.flush()

        return PersonalKnowledgeIngestResult(
            document_id=document.id,
            job_id=job_id,
            source_sha256=clean_source_sha256,
            artifact_hash=artifact_hash,
            canonical_md_path=canonical_md_path,
            segment_count=len(segment_drafts),
            status=final_status,
            warnings=all_warnings,
        )

    async def ingest_source_bytes(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        filename: str,
        data: bytes,
        title: str | None = None,
        source_kind: str = "upload",
        source_uri: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        agent_searchable: bool = True,
        sensitivity: str = "internal",
        source_mime_type: str | None = None,
        doc_metadata: dict[str, Any] | None = None,
        source_sha256: str | None = None,
        attempt_increment: int = 1,
        managed_job_id: uuid.UUID | None = None,
    ) -> PersonalKnowledgeIngestResult:
        canonical_sensitivity = canonicalize_sensitivity(sensitivity).value
        safe_name = _safe_filename(filename)
        ext = _extension_for_filename(safe_name)
        source_hash = _validate_source_sha256(source_sha256) if source_sha256 is not None else _sha256_bytes(data)
        artifact_hash = source_hash
        media_kind = _media_kind_for_extension(ext)
        if media_kind is not None:
            return await self._ingest_media_bytes(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                filename=safe_name,
                data=data,
                title=title,
                source_kind=source_kind,
                source_uri=source_uri,
                created_by_user_id=created_by_user_id,
                agent_searchable=agent_searchable,
                sensitivity=canonical_sensitivity,
                source_mime_type=source_mime_type,
                source_hash=source_hash,
                media_kind=media_kind,
                doc_metadata=doc_metadata,
                attempt_increment=attempt_increment,
                managed_job_id=managed_job_id,
            )
        if ext not in _SUPPORTED_IMPORT_EXTENSIONS:
            return await self._record_failed_import(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                source_kind=source_kind,
                source_uri=source_uri,
                source_hash=source_hash,
                artifact_hash=artifact_hash,
                title=_clean_title(safe_name),
                sensitivity=canonical_sensitivity,
                agent_searchable=agent_searchable,
                created_by_user_id=created_by_user_id,
                stage="converting",
                error_message=f"unsupported_file_type:{ext or 'unknown'}",
                error_code="unsupported_file_type",
                metadata={
                    "source_filename": safe_name,
                    "source_mime_type": source_mime_type or "",
                    "error": "unsupported_file_type",
                    **(doc_metadata or {}),
                },
                managed_job_id=managed_job_id,
            )

        workspace_root = _personal_knowledge_root(self.data_root, owner_user_id)
        guessed_mime = source_mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        converter_override = self.conversion_service
        try:
            # The default production path converts in a killable child process
            # under an explicit physical timeout; a timeout is a typed
            # retryable failure and the spooled source evidence stays recorded.
            # An injected converter is test-only DI and keeps the thread path.
            if converter_override is not None:
                conversion = await asyncio.wait_for(
                    asyncio.to_thread(
                        converter_override.convert_bytes,
                        data=data,
                        filename=safe_name,
                        workspace_root=workspace_root,
                        source_uri=source_uri,
                        source_mime_type=guessed_mime,
                        tenant_id=tenant_id,
                        agent_id=None,
                        user_id=owner_user_id,
                        mode="auto",
                        force_refresh=False,
                    ),
                    timeout=self.conversion_timeout_seconds,
                )
            else:
                from app.services.document_conversion import convert_bytes_in_killable_process

                conversion = await convert_bytes_in_killable_process(
                    data=data,
                    filename=safe_name,
                    workspace_root=workspace_root,
                    timeout_seconds=self.conversion_timeout_seconds,
                    source_uri=source_uri,
                    source_mime_type=guessed_mime,
                    tenant_id=tenant_id,
                    agent_id=None,
                    user_id=owner_user_id,
                    mode="auto",
                    force_refresh=False,
                )
        except asyncio.TimeoutError:
            return await self._record_failed_import(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                source_kind=source_kind,
                source_uri=source_uri,
                source_hash=source_hash,
                artifact_hash=artifact_hash,
                title=_clean_title(title_from_filename_or_uri(safe_name, source_uri, title)),
                sensitivity=canonical_sensitivity,
                agent_searchable=agent_searchable,
                created_by_user_id=created_by_user_id,
                stage="converting",
                error_message="conversion_timeout",
                error_code="conversion_timeout",
                metadata={
                    "source_filename": safe_name,
                    "source_mime_type": guessed_mime,
                    "error": "conversion_timeout",
                    "retryable": True,
                    **(doc_metadata or {}),
                },
                managed_job_id=managed_job_id,
            )
        except Exception as exc:
            # The conversion boundary owns one exact typed code; the original
            # exception stays chained for operator logs and never becomes the
            # user-facing error state.
            raise PersonalKnowledgeConversionError("conversion_failed") from exc
        if not str(getattr(conversion, "markdown", "") or "").strip():
            raise PersonalKnowledgeConversionError("conversion_failed")
        warnings = list(getattr(conversion, "warnings", ()) or [])
        metadata = {
            "source_filename": safe_name,
            "source_kind": source_kind,
            "conversion_engine": getattr(conversion, "engine", "unknown"),
            "conversion_warnings": warnings,
            "conversion_markdown_path": getattr(conversion, "artifact_markdown_path", ""),
            "conversion_metadata_path": getattr(conversion, "artifact_metadata_path", ""),
            "source_mime_type": getattr(conversion, "source_mime_type", source_mime_type or ""),
            **(doc_metadata or {}),
        }
        return await self.ingest_markdown(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            title=title_from_filename_or_uri(safe_name, source_uri, title),
            markdown=str(getattr(conversion, "markdown", "") or ""),
            source_kind=source_kind,
            source_uri=source_uri,
            created_by_user_id=created_by_user_id,
            agent_searchable=agent_searchable,
            sensitivity=canonical_sensitivity,
            source_sha256=source_hash
            if source_sha256 is not None
            else getattr(conversion, "source_sha256", source_hash),
            doc_metadata=metadata,
            warnings=warnings,
            attempt_increment=attempt_increment,
            managed_job_id=managed_job_id,
        )

    async def _record_failed_import(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        source_kind: str,
        source_uri: str | None,
        source_hash: str,
        artifact_hash: str,
        title: str,
        sensitivity: str,
        agent_searchable: bool,
        created_by_user_id: uuid.UUID | None,
        stage: str,
        error_message: str,
        metadata: dict[str, Any],
        error_code: str | None = None,
        managed_job_id: uuid.UUID | None = None,
    ) -> PersonalKnowledgeIngestResult:
        canonical_sensitivity = canonicalize_sensitivity(sensitivity).value
        # Upsert by the source digest: a queued import that fails terminally
        # marks its own document failed instead of creating a duplicate row.
        existing_result = await session.execute(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.scope_type == "person",
                KnowledgeDocument.scope_id == owner_user_id,
                KnowledgeDocument.source_sha256 == source_hash,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        document = existing_result.scalar_one_or_none()
        if document is None:
            document = KnowledgeDocument(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                scope_type="person",
                scope_id=owner_user_id,
                owner_user_id=owner_user_id,
                source_kind=source_kind,
                source_uri=source_uri,
                source_sha256=source_hash,
                artifact_hash=artifact_hash,
                title=title,
                status="failed",
                sensitivity=canonical_sensitivity,
                agent_searchable=agent_searchable,
                canonical_md_path="",
                canonical_md_sha256=None,
                doc_metadata_json=dict(metadata),
                created_by_user_id=created_by_user_id,
            )
            session.add(document)
        else:
            # A transient (never-consumable) document terminalizes to failed;
            # an existing consumable or archived document keeps its status.
            if str(getattr(document, "status", "") or "") in {"queued", "running", "failed"}:
                document.status = "failed"
            document.doc_metadata_json = {**(getattr(document, "doc_metadata_json", {}) or {}), **dict(metadata)}
        await session.flush()
        job_id: uuid.UUID | None = managed_job_id
        if managed_job_id is None:
            job = KnowledgeIndexJob(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                document_id=document.id,
                scope_type="person",
                scope_id=owner_user_id,
                artifact_hash=artifact_hash,
                stage=stage,
                status="failed",
                error_message=error_message,
                attempt_count=1,
                job_metadata_json={
                    "source_kind": source_kind,
                    "source_sha256": source_hash,
                    "warnings": [error_message],
                    **dict(metadata),
                },
            )
            session.add(job)
            await session.flush()
            job_id = job.id
        return PersonalKnowledgeIngestResult(
            document_id=document.id,
            job_id=job_id,
            source_sha256=source_hash,
            artifact_hash=artifact_hash,
            canonical_md_path="",
            segment_count=0,
            status="failed",
            warnings=[error_message],
            error_code=error_code or error_message,
        )

    async def _ingest_media_bytes(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        filename: str,
        data: bytes,
        title: str | None,
        source_kind: str,
        source_uri: str | None,
        created_by_user_id: uuid.UUID | None,
        agent_searchable: bool,
        sensitivity: str,
        source_mime_type: str | None,
        source_hash: str,
        media_kind: str,
        doc_metadata: dict[str, Any] | None,
        attempt_increment: int = 1,
        managed_job_id: uuid.UUID | None = None,
    ) -> PersonalKnowledgeIngestResult:
        canonical_sensitivity = canonicalize_sensitivity(sensitivity).value
        provider = self._media_transcription_provider()
        base_metadata = {
            "source_filename": filename,
            "source_kind": source_kind,
            "source_mime_type": source_mime_type or mimetypes.guess_type(filename)[0] or "",
            "media_kind": media_kind,
            **(doc_metadata or {}),
        }
        if provider is None:
            return await self._record_failed_import(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                source_kind=source_kind,
                source_uri=source_uri,
                source_hash=source_hash,
                artifact_hash=source_hash,
                title=title_from_filename_or_uri(filename, source_uri, title),
                sensitivity=canonical_sensitivity,
                agent_searchable=agent_searchable,
                created_by_user_id=created_by_user_id,
                stage="transcribing",
                error_message="unsupported_or_unconfigured:media_transcription_provider",
                error_code="unsupported_or_unconfigured",
                metadata={**base_metadata, "error": "unsupported_or_unconfigured"},
                managed_job_id=managed_job_id,
            )

        call = provider.transcribe_media(
            data=data,
            filename=filename,
            source_mime_type=base_metadata["source_mime_type"],
            source_uri=source_uri,
            source_kind=source_kind,
            media_kind=media_kind,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            created_by_user_id=created_by_user_id,
        )
        transcript = await call if inspect.isawaitable(call) else call
        markdown = str(getattr(transcript, "markdown", None) or getattr(transcript, "transcript", "") or "").strip()
        if not markdown:
            return await self._record_failed_import(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                source_kind=source_kind,
                source_uri=source_uri,
                source_hash=source_hash,
                artifact_hash=source_hash,
                title=title_from_filename_or_uri(filename, source_uri, title),
                sensitivity=canonical_sensitivity,
                agent_searchable=agent_searchable,
                created_by_user_id=created_by_user_id,
                stage="transcribing",
                error_message="media_transcription_empty",
                error_code="media_transcription_empty",
                metadata={**base_metadata, "error": "media_transcription_empty"},
                managed_job_id=managed_job_id,
            )

        transcript_metadata = dict(getattr(transcript, "metadata", {}) or {})
        warnings = list(getattr(transcript, "warnings", ()) or [])
        media_metadata = {
            **base_metadata,
            **transcript_metadata,
            "media_provider": str(getattr(transcript, "provider", "") or provider.__class__.__name__),
            "media_duration_seconds": transcript_metadata.get("duration_seconds"),
            "media_cost_usd": transcript_metadata.get("cost_usd"),
            "media_warnings": warnings,
        }
        return await self.ingest_markdown(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            title=title_from_filename_or_uri(filename, source_uri, title),
            markdown=markdown,
            source_kind=source_kind,
            source_uri=source_uri,
            created_by_user_id=created_by_user_id,
            agent_searchable=agent_searchable,
            sensitivity=canonical_sensitivity,
            source_sha256=source_hash,
            doc_metadata=media_metadata,
            warnings=warnings,
            attempt_increment=attempt_increment,
            managed_job_id=managed_job_id,
        )

    async def ingest_url(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        url: str,
        title: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        agent_searchable: bool = True,
        sensitivity: str = "internal",
        source_sha256: str | None = None,
        attempt_increment: int = 1,
        managed_job_id: uuid.UUID | None = None,
    ) -> PersonalKnowledgeIngestResult:
        canonical_sensitivity = canonicalize_sensitivity(sensitivity).value
        clean_url = str(url or "").strip()
        from app.services.governed_egress import EgressLimits, fetch_public_http

        response = await fetch_public_http(
            clean_url,
            limits=EgressLimits(
                max_redirects=5,
                max_wire_bytes=32 * 1024 * 1024,
                max_decoded_bytes=32 * 1024 * 1024,
                total_timeout_seconds=20.0,
            ),
        )
        if response.status_code >= 400:
            raise ValueError(f"url import failed with HTTP {response.status_code}")
        final_url = response.url
        parsed = urlparse(final_url)
        filename = _safe_filename(Path(parsed.path).name or "imported-url.html")
        return await self.ingest_source_bytes(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            filename=filename,
            data=response.content,
            title=title,
            source_kind="url",
            source_uri=final_url,
            created_by_user_id=created_by_user_id,
            agent_searchable=agent_searchable,
            sensitivity=canonical_sensitivity,
            source_mime_type=response.headers.get("content-type"),
            source_sha256=source_sha256,
            attempt_increment=attempt_increment,
            managed_job_id=managed_job_id,
        )

    async def list_import_jobs(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        limit: int = 50,
    ) -> list[PersonalKnowledgeJobSummary]:
        result = await session.execute(
            select(KnowledgeIndexJob)
            .where(
                KnowledgeIndexJob.tenant_id == tenant_id,
                KnowledgeIndexJob.scope_type == "person",
                KnowledgeIndexJob.scope_id == owner_user_id,
            )
            .order_by(KnowledgeIndexJob.updated_at.desc(), KnowledgeIndexJob.created_at.desc())
            .limit(max(1, int(limit or 50)))
        )
        jobs = result.scalars().all()
        return [_job_lifecycle_view(job) for job in jobs]

    def _grant_summary(self, grant: Any) -> PersonalKnowledgeGrantSummary:
        now = datetime.now(timezone.utc)
        expires_at = getattr(grant, "expires_at", None)
        active = getattr(grant, "revoked_at", None) is None and (expires_at is None or expires_at > now)
        return PersonalKnowledgeGrantSummary(
            grant_id=grant.id,
            resource_type=str(grant.resource_type or "scope"),
            resource_id=grant.resource_id,
            document_id=grant.document_id,
            grantee_type=str(grant.grantee_type or ""),
            grantee_id=grant.grantee_id,
            permission=str(grant.permission or ""),
            requester_user_id=getattr(grant, "requester_user_id", None),
            session_id=str(getattr(grant, "session_id", "") or "") or None,
            purpose=str(getattr(grant, "purpose", "") or "") or None,
            delegation_id=str(getattr(grant, "delegation_id", "") or "") or None,
            sensitivity_ceiling=str(getattr(grant, "sensitivity_ceiling", "PL1_public") or "PL1_public"),
            binding_key=str(getattr(grant, "binding_key", "") or ""),
            expires_at=expires_at,
            revoked_at=getattr(grant, "revoked_at", None),
            revoked_by_user_id=getattr(grant, "revoked_by_user_id", None),
            active=active,
            metadata=dict(grant.grant_metadata_json or {}),
            created_at=grant.created_at,
        )

    async def list_personal_grants(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
        limit: int = 100,
    ) -> list[PersonalKnowledgeGrantSummary]:
        if current_user_id != owner_user_id:
            return []
        rows = (
            await session.execute(
                select(KnowledgeGrant)
                .where(
                    KnowledgeGrant.tenant_id == tenant_id,
                    KnowledgeGrant.scope_type == "person",
                    KnowledgeGrant.scope_id == owner_user_id,
                )
                .order_by(KnowledgeGrant.created_at.desc())
                .limit(max(1, int(limit or 100)))
            )
        ).all()
        grants: list[PersonalKnowledgeGrantSummary] = []
        for row in rows:
            grant = _row_first(row)
            if isinstance(grant, KnowledgeGrant) or hasattr(grant, "grant_metadata_json"):
                grants.append(self._grant_summary(grant))
        return grants

    async def create_personal_grant(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
        resource_type: str,
        resource_id: uuid.UUID | None,
        document_id: uuid.UUID | None,
        grantee_type: str,
        grantee_id: uuid.UUID,
        permission: str,
        requester_user_id: uuid.UUID | None = None,
        session_id: str | None = None,
        purpose: str | None = None,
        delegation_id: str | None = None,
        sensitivity_ceiling: str = "PL1_public",
        expires_at: datetime | None = None,
        grant_metadata: dict[str, Any] | None = None,
    ) -> PersonalKnowledgeGrantSummary | None:
        if current_user_id != owner_user_id:
            return None
        clean_resource_type = str(resource_type or "scope").strip().lower()
        clean_grantee_type = str(grantee_type or "").strip().lower()
        clean_permission = str(permission or "search").strip().lower()
        if clean_resource_type not in {"scope", "document"}:
            raise ValueError("resource_type must be scope or document")
        if clean_grantee_type not in {"user", "agent"}:
            raise ValueError("grantee_type must be user or agent; session is a binding on an agent grant")
        if clean_permission not in {"read", "search", "manage"}:
            raise ValueError("permission must be read, search, or manage")
        if clean_resource_type == "scope":
            if resource_id is not None and resource_id != owner_user_id:
                raise ValueError("scope resource_id must be the Personal Knowledge owner")
            if document_id is not None:
                raise ValueError("document_id is not valid for a scope grant")
            resolved_resource_id = owner_user_id
        else:
            resolved_resource_id = document_id or resource_id
            if document_id is not None and resource_id is not None and document_id != resource_id:
                raise ValueError("document_id and resource_id must match")
        if resolved_resource_id is None:
            raise ValueError("resource_id is required")

        canonical_ceiling = canonicalize_sensitivity(sensitivity_ceiling).value
        clean_session_id = str(session_id or "").strip() or None
        clean_purpose = str(purpose or "").strip().lower() or None
        clean_delegation_id = str(delegation_id or "").strip() or None
        resolved_requester_id = requester_user_id
        if clean_grantee_type == "agent":
            if clean_purpose not in _PERSONAL_AGENT_GRANT_PURPOSES:
                raise ValueError("purpose is required for an agent grant")
            if clean_purpose == "autonomous_agent":
                resolved_requester_id = resolved_requester_id or owner_user_id
                if resolved_requester_id != owner_user_id:
                    raise ValueError("autonomous_agent grants are restricted to the owner requester")
                if clean_session_id is not None:
                    raise ValueError("autonomous_agent grants cannot carry session_id")
                if clean_delegation_id is not None:
                    raise ValueError("autonomous_agent grants cannot carry delegation_id")
            else:
                if resolved_requester_id is None:
                    raise ValueError("requester_user_id is required for an agent grant")
                if clean_session_id is None:
                    raise ValueError("session_id is required for an interactive or delegated agent grant")
            if clean_purpose in _PERSONAL_DELEGATED_GRANT_PURPOSES and clean_delegation_id is None:
                raise ValueError("delegation_id is required for a delegated agent grant")
            if clean_purpose not in _PERSONAL_DELEGATED_GRANT_PURPOSES and clean_delegation_id is not None:
                raise ValueError("delegation_id is valid only for a delegated agent grant")
            if expires_at is None:
                raise ValueError("expires_at is required for an agent grant")
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ValueError("expires_at must include a timezone")
            if expires_at <= datetime.now(timezone.utc):
                raise ValueError("expires_at must be in the future")
        else:
            resolved_requester_id = None
            clean_session_id = None
            clean_purpose = None
            clean_delegation_id = None

        if clean_resource_type == "document":
            document_exists = (
                await session.execute(
                    select(KnowledgeDocument.id).where(
                        KnowledgeDocument.id == resolved_resource_id,
                        KnowledgeDocument.tenant_id == tenant_id,
                        KnowledgeDocument.scope_type == "person",
                        KnowledgeDocument.scope_id == owner_user_id,
                        KnowledgeDocument.status != "deleted",
                    )
                )
            ).scalar_one_or_none()
            if document_exists is None:
                raise ValueError("document does not belong to the Personal Knowledge owner")

        grantee_model = Agent if clean_grantee_type == "agent" else User
        grantee_tenant_id = getattr(grantee_model, "tenant_id")
        grantee_exists = (
            await session.execute(
                select(grantee_model.id).where(
                    grantee_model.id == grantee_id,
                    grantee_tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if grantee_exists is None:
            raise ValueError(f"{clean_grantee_type} grantee does not belong to the tenant")

        binding_payload = {
            "tenant_id": str(tenant_id),
            "owner_user_id": str(owner_user_id),
            "resource_type": clean_resource_type,
            "resource_id": str(resolved_resource_id),
            "grantee_type": clean_grantee_type,
            "grantee_id": str(grantee_id),
            "permission": clean_permission,
            "requester_user_id": str(resolved_requester_id) if resolved_requester_id else None,
            "session_id": clean_session_id,
            "purpose": clean_purpose,
            "delegation_id": clean_delegation_id,
            "sensitivity_ceiling": canonical_ceiling,
        }
        binding_key = f"pkb:{_sha256(json.dumps(binding_payload, sort_keys=True, separators=(',', ':')))}"

        result = await session.execute(
            select(KnowledgeGrant).where(
                KnowledgeGrant.tenant_id == tenant_id,
                KnowledgeGrant.scope_type == "person",
                KnowledgeGrant.scope_id == owner_user_id,
                KnowledgeGrant.resource_type == clean_resource_type,
                KnowledgeGrant.resource_id == resolved_resource_id,
                KnowledgeGrant.grantee_type == clean_grantee_type,
                KnowledgeGrant.grantee_id == grantee_id,
                KnowledgeGrant.permission == clean_permission,
                KnowledgeGrant.binding_key == binding_key,
            )
        )
        grant = result.scalar_one_or_none()
        created = not isinstance(grant, KnowledgeGrant)
        if created:
            grant = KnowledgeGrant(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                scope_type="person",
                scope_id=owner_user_id,
                resource_type=clean_resource_type,
                resource_id=resolved_resource_id,
                document_id=document_id if clean_resource_type == "document" else None,
                grantee_type=clean_grantee_type,
                grantee_id=grantee_id,
                permission=clean_permission,
                binding_key=binding_key,
                created_by_user_id=current_user_id,
            )
            session.add(grant)
        previous_revoked_at = getattr(grant, "revoked_at", None)
        grant.requester_user_id = resolved_requester_id
        grant.session_id = clean_session_id
        grant.purpose = clean_purpose
        grant.delegation_id = clean_delegation_id
        grant.sensitivity_ceiling = canonical_ceiling
        grant.binding_key = binding_key
        grant.expires_at = expires_at
        grant.revoked_at = None
        grant.revoked_by_user_id = None
        metadata = dict(grant_metadata or {})
        if previous_revoked_at is not None:
            metadata["reactivated_from_revoked_at"] = previous_revoked_at.isoformat()
        grant.grant_metadata_json = metadata
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                user_id=current_user_id,
                agent_id=grantee_id if clean_grantee_type == "agent" else None,
                action="personal_kb.grant.upserted",
                details={
                    "grant_id": str(grant.id),
                    "operation": (
                        "created" if created else "reactivated" if previous_revoked_at is not None else "updated"
                    ),
                    "resource_type": clean_resource_type,
                    "resource_id": str(resolved_resource_id),
                    "grantee_type": clean_grantee_type,
                    "grantee_id": str(grantee_id),
                    "permission": clean_permission,
                    "requester_user_id": str(resolved_requester_id) if resolved_requester_id else None,
                    "session_id": clean_session_id,
                    "purpose": clean_purpose,
                    "delegation_id": clean_delegation_id,
                    "sensitivity_ceiling": canonical_ceiling,
                    "expires_at": expires_at.isoformat() if expires_at is not None else None,
                    "binding_key": binding_key,
                },
            )
        )
        await session.flush()
        return self._grant_summary(grant)

    async def delete_personal_grant(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
        grant_id: uuid.UUID,
    ) -> bool:
        if current_user_id != owner_user_id:
            return False
        result = await session.execute(
            select(KnowledgeGrant).where(
                KnowledgeGrant.tenant_id == tenant_id,
                KnowledgeGrant.scope_type == "person",
                KnowledgeGrant.scope_id == owner_user_id,
                KnowledgeGrant.id == grant_id,
            )
        )
        grant = result.scalar_one_or_none()
        if grant is None:
            return False
        grant.revoked_at = datetime.now(timezone.utc)
        grant.revoked_by_user_id = current_user_id
        metadata = dict(grant.grant_metadata_json or {})
        metadata["authority_status"] = "revoked"
        metadata["revoked_at"] = grant.revoked_at.isoformat()
        grant.grant_metadata_json = metadata
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                user_id=current_user_id,
                agent_id=grant.grantee_id if str(grant.grantee_type or "") == "agent" else None,
                action="personal_kb.grant.revoked",
                details={
                    "grant_id": str(grant.id),
                    "resource_type": str(grant.resource_type or ""),
                    "resource_id": str(grant.resource_id),
                    "grantee_type": str(grant.grantee_type or ""),
                    "grantee_id": str(grant.grantee_id),
                    "permission": str(grant.permission or ""),
                    "requester_user_id": (
                        str(grant.requester_user_id) if grant.requester_user_id is not None else None
                    ),
                    "session_id": str(grant.session_id or "") or None,
                    "purpose": str(grant.purpose or "") or None,
                    "delegation_id": str(grant.delegation_id or "") or None,
                    "sensitivity_ceiling": str(grant.sensitivity_ceiling or ""),
                    "revoked_at": grant.revoked_at.isoformat(),
                    "binding_key": str(grant.binding_key or ""),
                },
            )
        )
        await session.flush()
        return True

    async def list_personal_graph(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
        limit: int = 100,
    ) -> PersonalKnowledgeGraphSummary:
        if current_user_id != owner_user_id:
            return PersonalKnowledgeGraphSummary(entities=[], links=[], assertions=[])
        clean_limit = max(1, min(300, int(limit or 100)))
        entity_rows = (
            await session.execute(
                select(KnowledgeEntity)
                .where(
                    KnowledgeEntity.tenant_id == tenant_id,
                    KnowledgeEntity.scope_type == "person",
                    KnowledgeEntity.scope_id == owner_user_id,
                    KnowledgeEntity.merged_into_entity_id.is_(None),
                )
                .order_by(KnowledgeEntity.confidence.desc(), KnowledgeEntity.updated_at.desc())
                .limit(clean_limit)
            )
        ).all()
        link_rows = (
            await session.execute(
                select(KnowledgeLink)
                .where(
                    KnowledgeLink.tenant_id == tenant_id,
                    KnowledgeLink.scope_type == "person",
                    KnowledgeLink.scope_id == owner_user_id,
                )
                .order_by(KnowledgeLink.confidence.desc(), KnowledgeLink.created_at.desc())
                .limit(clean_limit * 2)
            )
        ).all()
        assertion_rows = (
            await session.execute(
                select(KnowledgeAssertion)
                .where(
                    KnowledgeAssertion.tenant_id == tenant_id,
                    KnowledgeAssertion.scope_type == "person",
                    KnowledgeAssertion.scope_id == owner_user_id,
                    KnowledgeAssertion.status == "active",
                )
                .order_by(KnowledgeAssertion.confidence.desc(), KnowledgeAssertion.updated_at.desc())
                .limit(clean_limit)
            )
        ).all()
        entities: list[PersonalKnowledgeGraphEntity] = []
        for row in entity_rows:
            entity = _row_first(row)
            if not hasattr(entity, "canonical_name"):
                continue
            entities.append(
                PersonalKnowledgeGraphEntity(
                    entity_id=entity.id,
                    canonical_name=str(entity.canonical_name or ""),
                    entity_type=str(entity.entity_type or "freeform"),
                    aliases=[str(alias) for alias in list(entity.aliases_json or []) if str(alias).strip()],
                    description=entity.description,
                    confidence=float(entity.confidence or 0.0),
                    source_refs=[dict(ref) for ref in list(entity.source_refs_json or []) if isinstance(ref, dict)],
                )
            )
        links: list[PersonalKnowledgeGraphLink] = []
        for row in link_rows:
            link = _row_first(row)
            if not hasattr(link, "relation"):
                continue
            links.append(
                PersonalKnowledgeGraphLink(
                    link_id=link.id,
                    from_kind=str(link.from_kind or ""),
                    from_id=link.from_id,
                    to_kind=str(link.to_kind or ""),
                    to_id=link.to_id,
                    relation=str(link.relation or ""),
                    confidence=float(link.confidence or 0.0),
                    source_refs=[dict(ref) for ref in list(link.source_refs_json or []) if isinstance(ref, dict)],
                )
            )
        assertions: list[PersonalKnowledgeGraphAssertion] = []
        for row in assertion_rows:
            assertion = _row_first(row)
            if not hasattr(assertion, "predicate"):
                continue
            assertions.append(
                PersonalKnowledgeGraphAssertion(
                    assertion_id=assertion.id,
                    subject_text=str(assertion.subject_text or ""),
                    predicate=str(assertion.predicate or ""),
                    object_text=str(assertion.object_text or ""),
                    confidence=float(assertion.confidence or 0.0),
                    status=str(assertion.status or ""),
                    source_refs=[dict(ref) for ref in list(assertion.source_refs_json or []) if isinstance(ref, dict)],
                )
            )
        return PersonalKnowledgeGraphSummary(entities=entities, links=links, assertions=assertions)

    async def patch_personal_document(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
        agent_id: uuid.UUID | None,
        agent_searchable: bool | None = None,
        sensitivity: str | None = None,
        status: str | None = None,
    ) -> PersonalKnowledgeDocumentSummary | None:
        if current_user_id != owner_user_id:
            return None
        result = await session.execute(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.scope_type == "person",
                KnowledgeDocument.scope_id == owner_user_id,
                KnowledgeDocument.id == document_id,
            )
            # Serialize with a running import/rebuild: the row lock makes the
            # archive read the worker's final committed state (never a stale
            # pre-worker snapshot that would clobber fresh metadata).
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        document = result.scalar_one_or_none()
        if document is None:
            return None
        if agent_searchable is not None:
            document.agent_searchable = bool(agent_searchable)
        if sensitivity is not None:
            document.sensitivity = canonicalize_sensitivity(sensitivity).value
        if status is not None:
            clean_status = str(status or "").strip().lower()
            if clean_status != "archived":
                raise ValueError(f"unsupported_status:{clean_status or 'empty'}")
            metadata = dict(getattr(document, "doc_metadata_json", {}) or {})
            previous = str(getattr(document, "status", "") or "")
            if "archived_from_status" not in metadata and previous in {"ready", "degraded"}:
                metadata["archived_from_status"] = previous
            metadata["archived_at"] = datetime.now(timezone.utc).isoformat()
            document.doc_metadata_json = metadata
            document.status = "archived"
        await session.flush()
        if status is not None:
            # onupdate columns expire on flush; reload them inside the session
            # so the summary never triggers lazy IO outside greenlet context.
            refresh = getattr(session, "refresh", None)
            if refresh is not None:
                await refresh(document)
        return self._document_summary(owner_user_id=owner_user_id, document=document, segment_count=0)

    async def restore_personal_document(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
    ) -> PersonalKnowledgeDocumentSummary | None:
        """Restore an archived document to its previous consumable state.

        The archived_from_status marker (written by archive) is the authority;
        when it is missing, a document with live segments restores to degraded
        — never an invented ready without consumable evidence.
        """
        if current_user_id != owner_user_id:
            return None
        result = await session.execute(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.scope_type == "person",
                KnowledgeDocument.scope_id == owner_user_id,
                KnowledgeDocument.id == document_id,
            )
            # Same serialization boundary as archive: a restore never reads a
            # stale pre-worker snapshot while an import/rebuild is in flight.
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        document = result.scalar_one_or_none()
        if document is None:
            return None
        if str(getattr(document, "status", "") or "") != "archived":
            raise PersonalKnowledgeDocumentConflict("restore_requires_archived")
        metadata = dict(getattr(document, "doc_metadata_json", {}) or {})
        previous = str(metadata.get("archived_from_status") or "").strip().lower()
        if previous in {"ready", "degraded"}:
            document.status = previous
        else:
            segment_count = (
                await session.execute(
                    select(func.count())
                    .select_from(KnowledgeSegment)
                    .where(KnowledgeSegment.document_id == document.id)
                )
            ).scalar_one()
            if int(segment_count or 0) <= 0:
                raise PersonalKnowledgeDocumentConflict("restore_no_consumable_state")
            document.status = "degraded"
        metadata.pop("archived_from_status", None)
        metadata.pop("archived_at", None)
        metadata["restored_at"] = datetime.now(timezone.utc).isoformat()
        document.doc_metadata_json = metadata
        await session.flush()
        # onupdate columns expire on flush; reload inside the session.
        refresh = getattr(session, "refresh", None)
        if refresh is not None:
            await refresh(document)
        segment_count = (
            await session.execute(
                select(func.count()).select_from(KnowledgeSegment).where(KnowledgeSegment.document_id == document.id)
            )
        ).scalar_one()
        return self._document_summary(
            owner_user_id=owner_user_id, document=document, segment_count=int(segment_count or 0)
        )

    async def rebuild_personal_document_index(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
        attempt_increment: int = 1,
        managed_job_id: uuid.UUID | None = None,
    ) -> PersonalKnowledgeIngestResult | None:
        if current_user_id != owner_user_id:
            return None
        result = await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.scope_type == "person",
                KnowledgeDocument.scope_id == owner_user_id,
                KnowledgeDocument.id == document_id,
            )
        )
        document = result.scalar_one_or_none()
        if document is None:
            return None
        artifact_path = self.data_root / str(document.canonical_md_path or "")
        if not artifact_path.exists():
            job_id: uuid.UUID | None = managed_job_id
            if managed_job_id is None:
                job = KnowledgeIndexJob(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    document_id=document.id,
                    scope_type="person",
                    scope_id=owner_user_id,
                    artifact_hash=str(document.artifact_hash or document.source_sha256),
                    stage="indexing",
                    status="failed",
                    error_message="canonical_markdown_missing",
                    attempt_count=1,
                    job_metadata_json={"source_kind": document.source_kind},
                )
                session.add(job)
                await session.flush()
                job_id = job.id
            return PersonalKnowledgeIngestResult(
                document_id=document.id,
                job_id=job_id,
                source_sha256=str(document.source_sha256),
                artifact_hash=str(document.artifact_hash or ""),
                canonical_md_path=str(document.canonical_md_path or ""),
                segment_count=0,
                status="failed",
                warnings=["canonical_markdown_missing"],
                error_code="canonical_markdown_missing",
            )
        return await self.ingest_markdown(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            title=str(document.title or "Untitled knowledge document"),
            markdown=artifact_path.read_text(encoding="utf-8"),
            source_kind=str(document.source_kind or "rebuild"),
            source_uri=document.source_uri,
            created_by_user_id=document.created_by_user_id,
            agent_searchable=bool(document.agent_searchable),
            sensitivity=str(document.sensitivity or "internal"),
            source_sha256=str(document.source_sha256),
            doc_metadata=dict(document.doc_metadata_json or {}),
            force_reindex=True,
            attempt_increment=attempt_increment,
            managed_job_id=managed_job_id,
        )

    async def retry_import_job(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        job_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
    ) -> PersonalKnowledgeJobSummary | None:
        """Requeue a failed/cancelled import job without running any work.

        The retry only commits the queued transition; the caller schedules the
        asynchronous worker. The claim increment owns attempt_count, so it is
        preserved here. Retrying obeys the attempt ceiling via a typed
        conflict instead of resurrecting a permanent queued loop.
        """
        if current_user_id != owner_user_id:
            return None
        result = await session.execute(
            select(KnowledgeIndexJob).where(
                KnowledgeIndexJob.tenant_id == tenant_id,
                KnowledgeIndexJob.scope_type == "person",
                KnowledgeIndexJob.scope_id == owner_user_id,
                KnowledgeIndexJob.id == job_id,
            )
        )
        job = result.scalar_one_or_none()
        if job is None:
            return None
        status = str(getattr(job, "status", "") or "").lower()
        max_attempts = int(
            dict(getattr(job, "job_metadata_json", {}) or {}).get("max_attempts") or _DEFAULT_IMPORT_JOB_MAX_ATTEMPTS
        )
        if status not in {"failed", "cancelled"}:
            raise PersonalKnowledgeJobConflict("not_retryable", retryable=False)
        if int(getattr(job, "attempt_count", 0) or 0) >= max_attempts:
            raise PersonalKnowledgeJobConflict("retry_attempt_limit", retryable=False)
        # Same authority as the read model: permanent failure codes (bad
        # input, missing evidence, exhausted budget) reject with a typed
        # conflict instead of a futile requeue.
        if _job_error_code(job) in _NON_RETRYABLE_JOB_ERROR_CODES:
            raise PersonalKnowledgeJobConflict("not_retryable", retryable=False)
        metadata = dict(getattr(job, "job_metadata_json", {}) or {})
        # One consistent timestamp across the CAS payload and the returned
        # summary — never two diverging now() calls. The requeued job also
        # drops its previous terminal current-state fields so the fresh
        # queued read model exposes no stale failure/cancel evidence.
        retried_at = datetime.now(timezone.utc).isoformat()
        cleaned_metadata = {
            key: value for key, value in metadata.items() if key not in _STALE_TERMINAL_JOB_METADATA_KEYS
        }
        queued_metadata = {**cleaned_metadata, "retried_at": retried_at}
        cas = await session.execute(
            update(KnowledgeIndexJob)
            .where(
                KnowledgeIndexJob.tenant_id == tenant_id,
                KnowledgeIndexJob.scope_type == "person",
                KnowledgeIndexJob.scope_id == owner_user_id,
                KnowledgeIndexJob.id == job_id,
                KnowledgeIndexJob.status == status,
            )
            .values(
                status="queued",
                stage="queued",
                error_message=None,
                job_metadata_json=queued_metadata,
            )
        )
        if int(getattr(cas, "rowcount", 0) or 0) != 1:  # pragma: no cover - race window
            raise PersonalKnowledgeJobConflict("not_retryable", retryable=False)
        job.status = "queued"
        job.stage = "queued"
        job.error_message = None
        job.job_metadata_json = queued_metadata
        await session.flush()
        # onupdate columns expire on flush; reload inside the session so the
        # summary never triggers lazy IO outside greenlet context.
        refresh = getattr(session, "refresh", None)
        if refresh is not None:
            await refresh(job)
        return _job_lifecycle_view(job)

    async def cancel_import_job(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        job_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
    ) -> PersonalKnowledgeJobSummary | None:
        """Cancel a queued import job via a compare-and-set transition.

        A running job cannot be cancelled race-safely without schema work, so
        the conflict is typed (cancellable=False) instead of pretending the
        cancellation succeeded; terminal jobs reject with a terminal conflict.
        """
        if current_user_id != owner_user_id:
            return None

        async def _load_job() -> Any | None:
            loaded = await session.execute(
                select(KnowledgeIndexJob).where(
                    KnowledgeIndexJob.tenant_id == tenant_id,
                    KnowledgeIndexJob.scope_type == "person",
                    KnowledgeIndexJob.scope_id == owner_user_id,
                    KnowledgeIndexJob.id == job_id,
                )
            )
            return loaded.scalar_one_or_none()

        job = await _load_job()
        if job is None:
            return None
        if str(getattr(job, "status", "") or "").lower() == "queued":
            metadata = dict(getattr(job, "job_metadata_json", {}) or {})
            # One committed timestamp: the CAS payload, the ORM row, and the
            # returned summary all carry the same cancelled_at.
            cancelled_at = datetime.now(timezone.utc).isoformat()
            cas = await session.execute(
                update(KnowledgeIndexJob)
                .where(
                    KnowledgeIndexJob.tenant_id == tenant_id,
                    KnowledgeIndexJob.scope_type == "person",
                    KnowledgeIndexJob.scope_id == owner_user_id,
                    KnowledgeIndexJob.id == job_id,
                    KnowledgeIndexJob.status == "queued",
                )
                .values(
                    status="cancelled",
                    stage="cancelled",
                    error_message=None,
                    job_metadata_json={**metadata, "cancelled_at": cancelled_at},
                )
            )
            if int(getattr(cas, "rowcount", 0) or 0) == 1:
                job.status = "cancelled"
                job.stage = "cancelled"
                job.error_message = None
                job.job_metadata_json = {**metadata, "cancelled_at": cancelled_at}
                # A cancelled initial import must not leave its document
                # permanently queued; a consumable rebuild document keeps its
                # status. Same transaction as the CAS above.
                await self._stage_transient_document_failure(
                    session, document_id=job.document_id, error_code="cancelled"
                )
                await session.flush()
                # onupdate columns expire on flush; reload inside the session.
                refresh = getattr(session, "refresh", None)
                if refresh is not None:
                    await refresh(job)
                return _job_lifecycle_view(job)
            job = await _load_job()  # pragma: no cover - CAS lost, re-read below
        status = str(getattr(job, "status", "") or "").lower()
        if status == "running":
            raise PersonalKnowledgeJobConflict("not_cancellable_while_running", cancellable=False)
        raise PersonalKnowledgeJobConflict("not_cancellable_terminal", cancellable=False)

    async def _ingest_queued_payload(
        self,
        session: Any,
        *,
        job: Any,
        metadata: dict[str, Any],
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        managed_job_id: uuid.UUID,
    ) -> PersonalKnowledgeIngestResult:
        """Run the queued import body (markdown / source bytes / url).

        Known infrastructure failures raise typed PersonalKnowledgeImportError
        codes; unexpected exceptions propagate to the typed mapping at the
        caller. The managed job row is never read or written here.
        """

        def _optional_uuid(value: Any) -> uuid.UUID | None:
            if value in {None, ""}:
                return None
            return uuid.UUID(str(value))

        queued_kind = str(metadata.get("queued_import_kind") or "").strip()
        source_hash = _validate_source_sha256(metadata.get("source_sha256") or getattr(job, "artifact_hash", ""))
        attempt_kwargs: dict[str, Any] = {"attempt_increment": 0, "managed_job_id": managed_job_id}
        if queued_kind == "markdown":
            queued_path = self.data_root / str(metadata.get("queued_markdown_path") or "")
            if not queued_path.exists():
                raise PersonalKnowledgeSourceMissingError("source_missing")
            return await self.ingest_markdown(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                title=str(metadata.get("title") or "Untitled knowledge document"),
                markdown=queued_path.read_text(encoding="utf-8"),
                source_kind=str(metadata.get("source_kind") or "paste"),
                source_uri=metadata.get("source_uri"),
                created_by_user_id=_optional_uuid(metadata.get("created_by_user_id")),
                agent_searchable=bool(metadata.get("agent_searchable", True)),
                sensitivity=str(metadata.get("sensitivity") or "internal"),
                source_sha256=source_hash,
                doc_metadata=dict(metadata.get("doc_metadata") or {}),
                force_reindex=True,
                **attempt_kwargs,
            )
        if queued_kind == "source_bytes":
            queued_path = self.data_root / str(metadata.get("queued_source_path") or "")
            if not queued_path.exists():
                raise PersonalKnowledgeSourceMissingError("source_missing")
            return await self.ingest_source_bytes(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                filename=str(metadata.get("source_filename") or queued_path.name),
                data=queued_path.read_bytes(),
                title=metadata.get("title"),
                source_kind=str(metadata.get("source_kind") or "upload"),
                source_uri=metadata.get("source_uri"),
                created_by_user_id=_optional_uuid(metadata.get("created_by_user_id")),
                agent_searchable=bool(metadata.get("agent_searchable", True)),
                sensitivity=str(metadata.get("sensitivity") or "internal"),
                source_mime_type=metadata.get("source_mime_type"),
                doc_metadata=dict(metadata.get("doc_metadata") or {}),
                source_sha256=source_hash,
                **attempt_kwargs,
            )
        if queued_kind == "url":
            return await self.ingest_url(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                url=str(metadata.get("source_uri") or ""),
                title=metadata.get("title"),
                created_by_user_id=_optional_uuid(metadata.get("created_by_user_id")),
                agent_searchable=bool(metadata.get("agent_searchable", True)),
                sensitivity=str(metadata.get("sensitivity") or "internal"),
                source_sha256=source_hash,
                **attempt_kwargs,
            )
        raise PersonalKnowledgeImportError("import_payload_invalid")

    async def _process_queued_import_job(
        self,
        session: Any,
        *,
        job: Any,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
        claimed: bool = False,
    ) -> PersonalKnowledgeIngestResult | None:
        if current_user_id != owner_user_id:
            return None
        metadata = dict(getattr(job, "job_metadata_json", {}) or {})
        queued_kind = str(metadata.get("queued_import_kind") or "").strip()
        if not queued_kind:
            return None

        source_hash = _validate_source_sha256(metadata.get("source_sha256") or getattr(job, "artifact_hash", ""))
        if not claimed:
            await self._claim_import_job_for_processing(session, job=job, metadata=metadata)
            metadata = dict(getattr(job, "job_metadata_json", {}) or metadata)

        try:
            # The worker claim already owns attempt accounting exactly once;
            # the ingest underneath must not increment the same job again.
            result = await self._ingest_queued_payload(
                session,
                job=job,
                metadata=metadata,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                managed_job_id=job.id,
            )
        except Exception as exc:
            # Exact typed taxonomy: known infrastructure failures carry their
            # code; anything unknown collapses to one generic safe code. Raw
            # exception prose never becomes the user-facing error state (the
            # class name stays as operator evidence).
            code = _typed_import_error_code(exc)
            warning = f"{code}:{exc.__class__.__name__}"
            job.stage = "failed"
            job.status = "failed"
            job.error_message = code
            job.job_metadata_json = {
                **metadata,
                "error": code,
                "warnings": [warning],
                "failure_exception": exc.__class__.__name__,
            }
            await session.flush()
            return PersonalKnowledgeIngestResult(
                document_id=job.document_id,
                job_id=job.id,
                source_sha256=source_hash,
                artifact_hash=str(getattr(job, "artifact_hash", source_hash) or source_hash),
                canonical_md_path="",
                segment_count=0,
                status="failed",
                warnings=[warning],
                error_code=code,
            )

        self._apply_import_job_result(job=job, metadata=metadata, result=result)
        await session.flush()
        return result

    async def _claim_import_job_for_processing(self, session: Any, *, job: Any, metadata: dict[str, Any]) -> None:
        job.stage = "processing"
        job.status = "running"
        job.error_message = None
        job.attempt_count = int(getattr(job, "attempt_count", 0) or 0) + 1
        # The opaque claim token plus the post-claim attempt count are the
        # lease identity: the fenced terminal CAS compares both against the
        # committed row before any phase-2 write may land.
        claimed_token = uuid.uuid4().hex
        job.job_metadata_json = {
            **metadata,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
            "claimed_token": claimed_token,
        }
        await session.flush()

    def _apply_import_job_result(
        self,
        *,
        job: Any,
        metadata: dict[str, Any],
        result: PersonalKnowledgeIngestResult,
    ) -> None:
        succeeded = result.status in {"ready", "degraded"}
        job.stage = "indexed" if succeeded else "failed"
        job.status = result.status
        job.error_message = None if succeeded else (result.error_code or ";".join(result.warnings or []))
        terminal_metadata = {
            **metadata,
            "warnings": list(result.warnings or []),
            "processed_result_job_id": str(result.job_id or job.id),
            "processed_document_id": str(result.document_id),
            "processed_canonical_md_path": result.canonical_md_path,
        }
        if succeeded:
            terminal_metadata.pop("error", None)
        elif result.error_code:
            terminal_metadata["error"] = result.error_code
        job.job_metadata_json = terminal_metadata

    async def _mark_import_job_attempt_limit_exceeded(self, session: Any, *, job: Any) -> None:
        warning = "personal_kb_import_attempt_limit_exceeded"
        metadata = dict(getattr(job, "job_metadata_json", {}) or {})
        job.stage = "failed"
        job.status = "failed"
        job.error_message = warning
        job.job_metadata_json = {**metadata, "error": warning, "warnings": [warning]}
        await session.flush()

    async def _fail_import_job_after_worker_error(
        self, session: Any, *, job: Any, metadata: dict[str, Any], code: str, exception_name: str
    ) -> None:
        warning = f"personal_kb_import_{code}:{exception_name}"
        job.stage = "failed"
        job.status = "failed"
        job.error_message = code
        job.job_metadata_json = {
            **metadata,
            "error": code,
            "warnings": [warning],
            "failure_exception": exception_name,
        }
        await session.flush()

    async def _fail_claimed_job_after_worker_error(
        self,
        session: Any,
        *,
        job_id: uuid.UUID,
        claimed_token: str,
        claimed_attempt: int,
        code: str,
        exception_name: str,
    ) -> bool:
        """Typed terminal failure for an unexpected phase-2 error, fenced by
        the claim lease (token + attempt + running). Returns False when the
        lease was lost — then neither the job row nor the document is
        mutated. On a successful CAS the transient (never-consumable)
        document terminalizes in the same transaction."""
        job = (
            await session.execute(select(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job_id))
        ).scalar_one_or_none()
        if job is None:
            return False
        warning = f"personal_kb_import_{code}:{exception_name}"
        metadata = {
            **dict(getattr(job, "job_metadata_json", {}) or {}),
            "error": code,
            "warnings": [warning],
            "failure_exception": exception_name,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        cas = await session.execute(
            update(KnowledgeIndexJob)
            .where(
                KnowledgeIndexJob.id == job_id,
                KnowledgeIndexJob.status == "running",
                KnowledgeIndexJob.attempt_count == int(claimed_attempt),
                KnowledgeIndexJob.job_metadata_json["claimed_token"].astext == claimed_token,
            )
            .values(status="failed", stage="failed", error_message=code, job_metadata_json=metadata)
            .execution_options(synchronize_session=False)
        )
        if int(getattr(cas, "rowcount", 0) or 0) != 1:
            return False
        await self._stage_transient_document_failure(session, document_id=job.document_id, error_code=code)
        return True

    async def process_import_jobs(
        self,
        session: Any = None,
        *,
        session_factory: Any = None,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
        limit: int = 10,
        statuses: tuple[str, ...] = ("queued",),
        queued_grace_seconds: int = _DEFAULT_IMPORT_JOB_QUEUED_GRACE_SECONDS,
        running_timeout_seconds: int | None = None,
        max_attempts: int = _DEFAULT_IMPORT_JOB_MAX_ATTEMPTS,
    ) -> PersonalKnowledgeJobProcessSummary:
        if current_user_id != owner_user_id:
            return PersonalKnowledgeJobProcessSummary(attempted=0, succeeded=0, failed=0, skipped=0, results=[])
        # Workers select queued jobs and stale-running leases only. A failed
        # job re-enters queued exclusively through the explicit retry CAS.
        clean_statuses = tuple(
            status
            for status in (str(item or "").strip().lower() for item in statuses)
            if status in {"queued", "running"}
        ) or ("queued",)
        now = datetime.now(timezone.utc)
        queued_before = now - timedelta(seconds=max(0, int(queued_grace_seconds or 0)))
        running_before = (
            now - timedelta(seconds=max(0, int(running_timeout_seconds)))
            if running_timeout_seconds is not None
            else None
        )
        if session_factory is not None:
            return await self._process_import_jobs_two_phase(
                session_factory,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                limit=limit,
                statuses=clean_statuses,
                queued_before=queued_before,
                running_before=running_before,
                max_attempts=max_attempts,
            )
        rows = (
            await session.execute(
                build_personal_knowledge_job_claim_statement(
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    statuses=clean_statuses,
                    queued_before=queued_before,
                    running_before=running_before,
                    max_attempts=max_attempts,
                    limit=limit,
                )
            )
        ).all()
        return await self._process_claimed_import_job_rows(
            session,
            rows=rows,
            max_attempts=max_attempts,
            default_tenant_id=tenant_id,
            default_owner_user_id=owner_user_id,
        )

    async def claim_and_process_stuck_jobs(
        self,
        session: Any = None,
        *,
        session_factory: Any = None,
        limit: int = 10,
        queued_grace_seconds: int = 30,
        running_timeout_seconds: int = _DEFAULT_IMPORT_JOB_RUNNING_TIMEOUT_SECONDS,
        max_attempts: int = _DEFAULT_IMPORT_JOB_MAX_ATTEMPTS,
    ) -> PersonalKnowledgeJobProcessSummary:
        now = datetime.now(timezone.utc)
        if session_factory is not None:
            return await self._process_import_jobs_two_phase(
                session_factory,
                tenant_id=None,
                owner_user_id=None,
                limit=limit,
                statuses=("queued", "running"),
                queued_before=now - timedelta(seconds=max(0, int(queued_grace_seconds or 0))),
                running_before=now - timedelta(seconds=max(0, int(running_timeout_seconds or 0))),
                max_attempts=max_attempts,
            )
        rows = (
            await session.execute(
                build_personal_knowledge_job_claim_statement(
                    tenant_id=None,
                    owner_user_id=None,
                    statuses=("queued", "running"),
                    queued_before=now - timedelta(seconds=max(0, int(queued_grace_seconds or 0))),
                    running_before=now - timedelta(seconds=max(0, int(running_timeout_seconds or 0))),
                    max_attempts=max_attempts,
                    limit=limit,
                )
            )
        ).all()
        return await self._process_claimed_import_job_rows(
            session,
            rows=rows,
            max_attempts=max_attempts,
            default_tenant_id=None,
            default_owner_user_id=None,
        )

    async def _process_import_jobs_two_phase(
        self,
        session_factory: Any,
        *,
        tenant_id: uuid.UUID | None,
        owner_user_id: uuid.UUID | None,
        limit: int,
        statuses: tuple[str, ...],
        queued_before: datetime,
        running_before: datetime | None,
        max_attempts: int,
    ) -> PersonalKnowledgeJobProcessSummary:
        """Durable two-phase claim loop — the live worker path.

        Phase 1 claims exactly one job in a short transaction (SKIP LOCKED
        select, status=running + attempt increment + opaque claim token,
        COMMIT) so the row lock is released and concurrent readers observe the
        real running state before long conversion/indexing starts. Phase 2
        stages document/segment writes only and finalizes through one fenced
        compare-and-set on the claim lease; a stale (reclaimed) lease ends as
        typed claim_lost with every staged write rolled back. A crash between
        the phases leaves a committed running job that the stale drain either
        reclaims (attempts left) or terminalizes as attempt-limit-exhausted.
        """
        results: list[dict[str, Any]] = []
        succeeded = 0
        failed = 0
        skipped = 0
        attempted = 0
        while attempted < max(1, int(limit or 1)):
            claim_statement = build_personal_knowledge_job_claim_statement(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                statuses=statuses,
                queued_before=queued_before,
                running_before=running_before,
                max_attempts=max_attempts,
                limit=1,
            )
            async with session_factory() as claim_session:
                rows = (await claim_session.execute(claim_statement)).all()
                if not rows:
                    break
                job = _row_first(rows[0])
                job_id = getattr(job, "id", None)
                document_id = getattr(job, "document_id", None)
                job_tenant = getattr(job, "tenant_id", None) or tenant_id
                job_owner = getattr(job, "scope_id", None) or owner_user_id
                if document_id is None or job_tenant is None or job_owner is None:
                    # Unreachable by schema (columns are non-nullable); a
                    # corrupt row must still terminalize instead of spinning
                    # in queued forever. The claim transaction holds the row
                    # lock, so this mutation is race-safe.
                    attempted += 1
                    job.stage = "failed"
                    job.status = "failed"
                    job.error_message = "import_payload_invalid"
                    job.job_metadata_json = {
                        **dict(getattr(job, "job_metadata_json", {}) or {}),
                        "error": "import_payload_invalid",
                        "warnings": ["import_payload_invalid"],
                    }
                    await claim_session.commit()
                    failed += 1
                    results.append(
                        {
                            "job_id": str(job_id or ""),
                            "document_id": str(document_id or ""),
                            "status": "failed",
                            "segment_count": 0,
                            "warnings": ["import_payload_invalid"],
                        }
                    )
                    continue
                metadata = dict(getattr(job, "job_metadata_json", {}) or {})
                attempt_ceiling = max(1, int(max_attempts or _DEFAULT_IMPORT_JOB_MAX_ATTEMPTS))
                if (
                    str(getattr(job, "status", "") or "") == "running"
                    and int(getattr(job, "attempt_count", 0) or 0) >= attempt_ceiling
                ):
                    # Crash after the final allowed claim: terminalize in this
                    # same short transaction WITHOUT consuming another attempt
                    # (the SKIP LOCKED row lock makes it race-safe). The
                    # never-consumable document terminalizes with it.
                    attempted += 1
                    code = "personal_kb_import_attempt_limit_exceeded"
                    job.stage = "failed"
                    job.status = "failed"
                    job.error_message = code
                    job.job_metadata_json = {
                        **metadata,
                        "error": code,
                        "warnings": [code],
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    }
                    await self._stage_transient_document_failure(
                        claim_session, document_id=document_id, error_code=code
                    )
                    await claim_session.commit()
                    failed += 1
                    results.append(
                        {
                            "job_id": str(job_id or ""),
                            "document_id": str(document_id or ""),
                            "status": "failed",
                            "segment_count": 0,
                            "warnings": [code],
                        }
                    )
                    continue
                # Only queued or stale-running rows below the ceiling receive
                # a fresh claim token and the single attempt increment.
                await self._claim_import_job_for_processing(claim_session, job=job, metadata=metadata)
                await claim_session.commit()
                claimed_metadata = dict(getattr(job, "job_metadata_json", {}) or metadata)
                claimed_token = str(claimed_metadata.get("claimed_token") or "")
                claimed_attempt = int(getattr(job, "attempt_count", 0) or 0)
                claimed_id = getattr(job, "id", None)
                claimed_document_id = getattr(job, "document_id", None)
            attempted += 1
            try:
                async with session_factory() as work_session:
                    result = await self._process_claimed_job_phase_two(
                        work_session,
                        job_id=claimed_id,
                        claimed_token=claimed_token,
                        claimed_attempt=claimed_attempt,
                        tenant_id=job_tenant,
                        owner_user_id=job_owner,
                        max_attempts=max_attempts,
                    )
                    await work_session.commit()
            except PersonalKnowledgeClaimLost:
                skipped += 1
                results.append(
                    {
                        "job_id": str(claimed_id or ""),
                        "document_id": str(claimed_document_id or ""),
                        "status": "claim_lost",
                        "segment_count": 0,
                        "warnings": ["personal_kb_import_claim_lost"],
                    }
                )
                continue
            except Exception as exc:
                wrote_failure = False
                async with session_factory() as fail_session:
                    wrote_failure = await self._fail_claimed_job_after_worker_error(
                        fail_session,
                        job_id=claimed_id,
                        claimed_token=claimed_token,
                        claimed_attempt=claimed_attempt,
                        code="worker_error",
                        exception_name=exc.__class__.__name__,
                    )
                    await fail_session.commit()
                if not wrote_failure:
                    skipped += 1
                    results.append(
                        {
                            "job_id": str(claimed_id or ""),
                            "document_id": str(claimed_document_id or ""),
                            "status": "claim_lost",
                            "segment_count": 0,
                            "warnings": ["personal_kb_import_claim_lost"],
                        }
                    )
                    continue
                failed += 1
                results.append(
                    {
                        "job_id": str(claimed_id or ""),
                        "document_id": str(claimed_document_id or ""),
                        "status": "failed",
                        "segment_count": 0,
                        "warnings": [f"personal_kb_import_worker_error:{exc.__class__.__name__}"],
                    }
                )
                continue
            if result is None:
                # The job row disappeared between claim and phase 2 — nothing
                # remains to finalize, and no running row is left behind.
                skipped += 1
                results.append({"job_id": str(claimed_id or ""), "status": "skipped", "warnings": ["job_deleted"]})
                continue
            terminal_status = str(result.status or "")
            if terminal_status in {"ready", "degraded"}:
                succeeded += 1
            else:
                failed += 1
            results.append(
                {
                    "job_id": str(claimed_id or result.job_id or ""),
                    "document_id": str(result.document_id),
                    "status": terminal_status,
                    "segment_count": result.segment_count,
                    "warnings": list(result.warnings or []),
                }
            )
        return PersonalKnowledgeJobProcessSummary(
            attempted=attempted,
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            results=results,
        )

    async def _process_claimed_job_phase_two(
        self,
        session: Any,
        *,
        job_id: uuid.UUID,
        claimed_token: str,
        claimed_attempt: int,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        max_attempts: int,
    ) -> PersonalKnowledgeIngestResult | None:
        """Execute the claimed job body and fence the terminal transition.

        Until the final compare-and-set, the managed KnowledgeIndexJob row is
        never mutated or flushed — the ingest stages document/segment writes
        only (managed_job_id mode). A zero-row CAS means another worker
        reclaimed the stale lease: PersonalKnowledgeClaimLost is raised so the
        caller rolls every staged write back and reports typed claim_lost.
        """
        job = (
            await session.execute(select(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job_id))
        ).scalar_one_or_none()
        if job is None:
            return None
        metadata = dict(getattr(job, "job_metadata_json", {}) or {})
        if (
            str(metadata.get("claimed_token") or "") != claimed_token
            or int(getattr(job, "attempt_count", 0) or 0) != int(claimed_attempt)
            or str(getattr(job, "status", "") or "") != "running"
        ):
            raise PersonalKnowledgeClaimLost(claimed_token)

        attempt_ceiling = max(1, int(max_attempts or _DEFAULT_IMPORT_JOB_MAX_ATTEMPTS))
        failure_exception: str | None = None
        if int(claimed_attempt) > attempt_ceiling:
            # Terminalize-only claim (stale-running at the attempt ceiling,
            # e.g. a crash after the final allowed claim): no work reruns.
            result = PersonalKnowledgeIngestResult(
                document_id=job.document_id,
                job_id=job.id,
                source_sha256=str(getattr(job, "artifact_hash", "") or ""),
                artifact_hash=str(getattr(job, "artifact_hash", "") or ""),
                canonical_md_path="",
                segment_count=0,
                status="failed",
                warnings=["personal_kb_import_attempt_limit_exceeded"],
                error_code="personal_kb_import_attempt_limit_exceeded",
            )
        else:
            try:
                result = await self._execute_claimed_body(
                    session,
                    job=job,
                    metadata=metadata,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                )
            except PersonalKnowledgeClaimLost:
                raise
            except Exception as exc:
                code = _typed_import_error_code(exc)
                failure_exception = exc.__class__.__name__
                result = PersonalKnowledgeIngestResult(
                    document_id=job.document_id,
                    job_id=job.id,
                    source_sha256=str(getattr(job, "artifact_hash", "") or ""),
                    artifact_hash=str(getattr(job, "artifact_hash", "") or ""),
                    canonical_md_path="",
                    segment_count=0,
                    status="failed",
                    warnings=[f"{code}:{failure_exception}"],
                    error_code=code,
                )

        succeeded = result.status in {"ready", "degraded"}
        if not succeeded:
            await self._stage_transient_document_failure(
                session,
                document_id=result.document_id,
                error_code=result.error_code or "import_failed",
            )
        terminal_metadata = {
            **metadata,
            "warnings": list(result.warnings or []),
            "processed_result_job_id": str(result.job_id or job.id),
            "processed_document_id": str(result.document_id),
            "processed_canonical_md_path": result.canonical_md_path,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        if succeeded:
            terminal_metadata.pop("error", None)
        else:
            terminal_metadata["error"] = result.error_code or "import_failed"
        if failure_exception is not None:
            terminal_metadata["failure_exception"] = failure_exception

        cas = await session.execute(
            update(KnowledgeIndexJob)
            .where(
                KnowledgeIndexJob.id == job_id,
                KnowledgeIndexJob.status == "running",
                KnowledgeIndexJob.attempt_count == int(claimed_attempt),
                KnowledgeIndexJob.job_metadata_json["claimed_token"].astext == claimed_token,
            )
            .values(
                status=str(result.status),
                stage="indexed" if succeeded else "failed",
                error_message=None if succeeded else (result.error_code or "import_failed"),
                job_metadata_json=terminal_metadata,
            )
            .execution_options(synchronize_session=False)
        )
        if int(getattr(cas, "rowcount", 0) or 0) != 1:
            raise PersonalKnowledgeClaimLost(claimed_token)
        return result

    async def _execute_claimed_body(
        self,
        session: Any,
        *,
        job: Any,
        metadata: dict[str, Any],
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> PersonalKnowledgeIngestResult:
        """Run the claimed job body with the job row in managed mode."""
        if metadata.get("queued_import_kind"):
            return await self._ingest_queued_payload(
                session,
                job=job,
                metadata=metadata,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                managed_job_id=job.id,
            )
        result = await self.rebuild_personal_document_index(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            document_id=job.document_id,
            current_user_id=owner_user_id,
            attempt_increment=0,
            managed_job_id=job.id,
        )
        if result is None:
            # The document is no longer readable for this scope — a typed
            # terminal failure, never an indefinitely running job.
            return PersonalKnowledgeIngestResult(
                document_id=job.document_id,
                job_id=job.id,
                source_sha256=str(getattr(job, "artifact_hash", "") or ""),
                artifact_hash=str(getattr(job, "artifact_hash", "") or ""),
                canonical_md_path="",
                segment_count=0,
                status="failed",
                warnings=["document_missing"],
                error_code="document_missing",
            )
        return result

    async def _stage_transient_document_failure(self, session: Any, *, document_id: uuid.UUID, error_code: str) -> None:
        """A terminal job failure must not leave a never-consumable document
        queued in the read model. A consumable (ready/degraded) document being
        rebuilt keeps its prior status. Staged in the work transaction — a
        lost claim CAS rolls this back with everything else."""
        document = (
            await session.execute(
                select(KnowledgeDocument)
                .where(KnowledgeDocument.id == document_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if document is None:
            return
        if str(getattr(document, "status", "") or "") in {"queued", "running", "failed"}:
            document.status = "failed"
            document.doc_metadata_json = {
                **(getattr(document, "doc_metadata_json", {}) or {}),
                "error": error_code,
            }
            await session.flush()

    async def _process_claimed_import_job_rows(
        self,
        session: Any,
        *,
        rows: list[Any],
        max_attempts: int,
        default_tenant_id: uuid.UUID | None,
        default_owner_user_id: uuid.UUID | None,
    ) -> PersonalKnowledgeJobProcessSummary:
        results: list[dict[str, Any]] = []
        succeeded = 0
        failed = 0
        skipped = 0
        for row in rows:
            job = _row_first(row)
            job_id = getattr(job, "id", None)
            document_id = getattr(job, "document_id", None)
            if document_id is None:
                skipped += 1
                results.append({"job_id": str(job_id or ""), "status": "skipped", "warnings": ["missing_document_id"]})
                continue
            tenant_id = getattr(job, "tenant_id", None) or default_tenant_id
            owner_user_id = getattr(job, "scope_id", None) or default_owner_user_id
            if tenant_id is None or owner_user_id is None:
                skipped += 1
                results.append({"job_id": str(job_id or ""), "status": "skipped", "warnings": ["missing_scope"]})
                continue
            raw_status = str(getattr(job, "status", "") or "").lower()
            if raw_status not in {"queued", "running"}:
                # Workers never (re)select terminal jobs — a failed job
                # re-enters queued only through the explicit retry CAS.
                skipped += 1
                results.append({"job_id": str(job_id or ""), "status": "skipped", "warnings": ["status_not_claimable"]})
                continue
            if int(getattr(job, "attempt_count", 0) or 0) >= max(
                1, int(max_attempts or _DEFAULT_IMPORT_JOB_MAX_ATTEMPTS)
            ):
                await self._mark_import_job_attempt_limit_exceeded(session, job=job)
                failed += 1
                results.append(
                    {
                        "job_id": str(job_id or ""),
                        "document_id": str(document_id),
                        "status": "failed",
                        "segment_count": 0,
                        "warnings": ["personal_kb_import_attempt_limit_exceeded"],
                    }
                )
                continue
            metadata = dict(getattr(job, "job_metadata_json", {}) or {})
            try:
                await self._claim_import_job_for_processing(session, job=job, metadata=metadata)
                metadata = dict(getattr(job, "job_metadata_json", {}) or metadata)
                if dict(getattr(job, "job_metadata_json", {}) or {}).get("queued_import_kind"):
                    result = await self._process_queued_import_job(
                        session,
                        job=job,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        current_user_id=owner_user_id,
                        claimed=True,
                    )
                else:
                    result = await self.rebuild_personal_document_index(
                        session,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        document_id=document_id,
                        current_user_id=owner_user_id,
                        attempt_increment=0,
                        managed_job_id=job.id,
                    )
                    if result is not None:
                        self._apply_import_job_result(job=job, metadata=metadata, result=result)
                        await session.flush()
            except Exception as exc:
                # Isolate one poison job (corrupt metadata, empty canonical markdown,
                # malformed source digest) so it cannot starve the rest of the claimed
                # batch. The failure is recorded on the job row — the queryable
                # observability surface for this pipeline — and the loop moves on.
                await self._fail_import_job_after_worker_error(
                    session,
                    job=job,
                    metadata=metadata,
                    code="worker_error",
                    exception_name=exc.__class__.__name__,
                )
                failed += 1
                results.append(
                    {
                        "job_id": str(job_id or ""),
                        "document_id": str(document_id),
                        "status": "failed",
                        "segment_count": 0,
                        "warnings": [f"personal_kb_import_worker_error:{exc.__class__.__name__}"],
                    }
                )
                continue
            if result is None:
                skipped += 1
                results.append({"job_id": str(job_id or ""), "status": "skipped", "warnings": ["job_not_rebuilt"]})
                continue
            terminal_status = str(result.status or "")
            if terminal_status in {"ready", "degraded"}:
                succeeded += 1
            else:
                failed += 1
            results.append(
                {
                    "job_id": str(job_id or result.job_id or ""),
                    "document_id": str(result.document_id),
                    "status": terminal_status,
                    "segment_count": result.segment_count,
                    "warnings": list(result.warnings or []),
                }
            )
        return PersonalKnowledgeJobProcessSummary(
            attempted=len(rows),
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            results=results,
        )

    async def search_personal_with_authority(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        query: str,
        principal: PersonalKnowledgePrincipal,
        limit: int | None = None,
    ) -> PersonalKnowledgeSearchResult:
        """Return an explicit scope decision plus only model-safe search hits."""

        decision = await resolve_personal_knowledge_permission(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            principal=principal,
            action="search",
        )
        if not decision.allowed:
            return PersonalKnowledgeSearchResult(status="denied", hits=[], authority=decision)
        hits = await self.search_personal(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            query=query,
            principal=principal,
            limit=limit,
        )
        unavailable_credential_refs = sum(
            1
            for hit in hits
            if hit.sensitivity == SensitivityLevel.PL4_CREDENTIAL.value and not hit.credential_reference
        )
        safe_hits = [
            hit for hit in hits if hit.sensitivity != SensitivityLevel.PL4_CREDENTIAL.value or hit.credential_reference
        ]
        if safe_hits and unavailable_credential_refs:
            status = "partial"
        elif safe_hits:
            status = "ok"
        elif unavailable_credential_refs:
            status = "unavailable"
        else:
            status = "empty"
        return PersonalKnowledgeSearchResult(
            status=status,
            hits=safe_hits,
            authority=decision,
            warnings=(
                [f"credential_reference_unavailable:{unavailable_credential_refs}"]
                if unavailable_credential_refs
                else []
            ),
        )

    async def search_personal(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        query: str,
        principal: PersonalKnowledgePrincipal,
        limit: int | None = None,
    ) -> list[KnowledgeSearchHit]:
        clean_query = _WHITESPACE_RE.sub(" ", str(query or "").strip())
        if not clean_query:
            raise ValueError("query must not be empty")
        result_limit = max(1, int(limit)) if limit is not None else None
        candidate_limit = max(result_limit * 3, 10) if result_limit is not None else None

        candidates: dict[uuid.UUID, dict[str, Any]] = {}

        def add_candidate(
            *,
            segment: Any,
            document: Any,
            channel: str,
            rank: int,
            raw_score: float,
            channel_metadata: dict[str, Any] | None = None,
        ) -> None:
            segment_id = segment.id
            document_metadata = dict(getattr(document, "doc_metadata_json", None) or {})
            source_ref = f"kb://person/{owner_user_id}/documents/{document.id}#segment={segment.id}"
            entry = candidates.setdefault(
                segment_id,
                {
                    "segment": segment,
                    "document": document,
                    "source_ref": source_ref,
                    "channels": {},
                    "rrf": 0.0,
                    "boosts": {
                        "heat": _heat_boost(document_metadata),
                        "freshness": _freshness_boost(getattr(document, "updated_at", None)),
                    },
                },
            )
            channel_trace = entry["channels"].setdefault(channel, {"rank": rank, "raw_score": raw_score})
            channel_trace["rank"] = min(int(channel_trace["rank"]), int(rank))
            channel_trace["raw_score"] = max(float(channel_trace["raw_score"]), float(raw_score or 0.0))
            if channel_metadata:
                channel_trace.update(channel_metadata)
            entry["rrf"] += 1.0 / (60.0 + max(1, int(rank)))

        text_statement = build_personal_knowledge_search_statement(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            query=clean_query,
            principal=principal,
            limit=candidate_limit,
        )
        text_rows = (await session.execute(text_statement)).all()
        for rank, row in enumerate(text_rows, start=1):
            segment, document, score = row[0], row[1], row[2]
            add_candidate(segment=segment, document=document, channel="text", rank=rank, raw_score=float(score or 0.0))

        like_query = f"%{_escape_like(clean_query)}%"
        entity_statement = (
            select(KnowledgeEntity)
            .where(
                KnowledgeEntity.tenant_id == tenant_id,
                KnowledgeEntity.scope_type == "person",
                KnowledgeEntity.scope_id == owner_user_id,
                KnowledgeEntity.merged_into_entity_id.is_(None),
                or_(
                    KnowledgeEntity.canonical_name.ilike(like_query, escape="\\"),
                    cast(KnowledgeEntity.aliases_json, Text).ilike(like_query, escape="\\"),
                ),
            )
            .order_by(KnowledgeEntity.confidence.desc(), KnowledgeEntity.updated_at.desc())
        )
        if candidate_limit is not None:
            entity_statement = entity_statement.limit(candidate_limit)
        entity_rows = (await session.execute(entity_statement)).all()
        entities = [
            entity
            for row in entity_rows
            if isinstance((entity := _row_first(row)), KnowledgeEntity) or hasattr(entity, "source_refs_json")
        ]
        entity_segment_ids: list[uuid.UUID] = []
        for entity in entities:
            entity_segment_ids.extend(_source_ref_segment_ids(getattr(entity, "source_refs_json", None)))

        optional_vector_trace: dict[str, Any] = {
            "enabled": False,
            "status": "disabled",
            "reason": "provider_unconfigured",
        }
        vector_segment_scores: dict[uuid.UUID, float] = {}
        vector_segment_metadata: dict[uuid.UUID, dict[str, Any]] = {}
        vector_provider = self._vector_index_provider()
        if vector_provider is not None:
            provider_name = vector_provider.__class__.__name__
            try:
                vector_search_arguments = {
                    "tenant_id": tenant_id,
                    "owner_user_id": owner_user_id,
                    "query": clean_query,
                    "principal": principal.evidence(),
                }
                if candidate_limit is not None:
                    vector_search_arguments["limit"] = candidate_limit
                call = vector_provider.search_personal_segments(
                    **vector_search_arguments,
                )
                vector_hits = await call if inspect.isawaitable(call) else call
                for hit in list(vector_hits or []):
                    if isinstance(hit, dict):
                        raw_segment_id = hit.get("segment_id")
                        raw_score = hit.get("score", 0.0)
                        raw_metadata = hit.get("metadata") or {}
                    elif isinstance(hit, tuple):
                        raw_segment_id = hit[0] if len(hit) >= 1 else None
                        raw_score = hit[1] if len(hit) >= 2 else 0.0
                        raw_metadata = hit[2] if len(hit) >= 3 and isinstance(hit[2], dict) else {}
                    else:
                        raw_segment_id = getattr(hit, "segment_id", None)
                        raw_score = getattr(hit, "score", 0.0)
                        raw_metadata = getattr(hit, "metadata", {}) or {}
                    if raw_segment_id is None:
                        continue
                    try:
                        segment_id = uuid.UUID(str(raw_segment_id))
                    except (TypeError, ValueError):
                        continue
                    score = max(0.0, float(raw_score or 0.0))
                    if score <= 0.0:
                        continue
                    vector_segment_scores[segment_id] = max(vector_segment_scores.get(segment_id, 0.0), score)
                    vector_segment_metadata[segment_id] = dict(raw_metadata or {})
                optional_vector_trace = {
                    "enabled": True,
                    "status": "ready",
                    "provider": provider_name,
                    "candidate_count": len(vector_segment_scores),
                }
            except Exception as exc:
                optional_vector_trace = {
                    "enabled": True,
                    "status": "failed",
                    "provider": provider_name,
                    "error": f"optional_vector_search_failed:{exc}",
                }

        graph_segment_scores: dict[uuid.UUID, float] = {}
        graph_segment_hops: dict[uuid.UUID, int] = {}
        if entities:
            entity_ids = [entity.id for entity in entities if getattr(entity, "id", None) is not None]
            if entity_ids:
                graph_statement = (
                    select(KnowledgeLink)
                    .where(
                        KnowledgeLink.tenant_id == tenant_id,
                        KnowledgeLink.scope_type == "person",
                        KnowledgeLink.scope_id == owner_user_id,
                        KnowledgeLink.from_kind == "entity",
                        KnowledgeLink.to_kind == "entity",
                    )
                    .order_by(KnowledgeLink.confidence.desc())
                )
                if candidate_limit is not None:
                    graph_statement = graph_statement.limit(max(candidate_limit * 50, 250))
                graph_rows = (await session.execute(graph_statement)).all()
                adjacency: dict[str, list[str]] = {}
                links: list[Any] = []
                for row in graph_rows:
                    link = _row_first(row)
                    from_id = getattr(link, "from_id", None)
                    to_id = getattr(link, "to_id", None)
                    if from_id is None or to_id is None:
                        continue
                    from_key = str(from_id)
                    to_key = str(to_id)
                    adjacency.setdefault(from_key, [])
                    adjacency.setdefault(to_key, [])
                    adjacency[from_key].append(to_key)
                    adjacency[to_key].append(from_key)
                    links.append(link)

                if adjacency and links:
                    from app.memory.relation_graph import personalized_pagerank

                    seed_weights = {
                        str(entity.id): 1.0 / float(rank)
                        for rank, entity in enumerate(entities, start=1)
                        if getattr(entity, "id", None) is not None
                    }
                    ppr_scores = personalized_pagerank(adjacency, seed_weights)
                    seed_keys = set(seed_weights)
                    distances: dict[str, int] = {node_id: 0 for node_id in seed_keys if node_id in adjacency}
                    frontier = list(distances)
                    while frontier:
                        current = frontier.pop(0)
                        for neighbor in adjacency.get(current, []):
                            if neighbor in distances:
                                continue
                            distances[neighbor] = distances[current] + 1
                            frontier.append(neighbor)

                    for link in links:
                        if not hasattr(link, "source_refs_json"):
                            continue
                        from_key = str(getattr(link, "from_id", ""))
                        to_key = str(getattr(link, "to_id", ""))
                        endpoint_score = max(float(ppr_scores.get(from_key, 0.0)), float(ppr_scores.get(to_key, 0.0)))
                        if endpoint_score <= 0.0:
                            continue
                        confidence = _coerce_confidence(getattr(link, "confidence", 1.0))
                        link_score = endpoint_score * confidence
                        if link_score <= 0.0:
                            continue
                        known_distances = [
                            distance
                            for distance in (distances.get(from_key), distances.get(to_key))
                            if distance is not None
                        ]
                        hop_count = (min(known_distances) + 1) if known_distances else 1
                        for segment_id in _source_ref_segment_ids(getattr(link, "source_refs_json", None)):
                            graph_segment_scores[segment_id] = max(
                                graph_segment_scores.get(segment_id, 0.0), link_score
                            )
                            graph_segment_hops[segment_id] = min(
                                graph_segment_hops.get(segment_id, hop_count), hop_count
                            )

        graph_segment_ids = [
            segment_id
            for segment_id, _score in sorted(graph_segment_scores.items(), key=lambda item: item[1], reverse=True)
        ]
        vector_segment_ids = [
            segment_id
            for segment_id, _score in sorted(vector_segment_scores.items(), key=lambda item: item[1], reverse=True)
        ]
        fetch_segment_ids = list(dict.fromkeys([*entity_segment_ids, *graph_segment_ids, *vector_segment_ids]))
        if fetch_segment_ids:
            segment_rows = (
                await session.execute(
                    select(KnowledgeSegment, KnowledgeDocument)
                    .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeSegment.document_id)
                    .where(
                        KnowledgeDocument.tenant_id == tenant_id,
                        KnowledgeDocument.scope_type == "person",
                        KnowledgeDocument.scope_id == owner_user_id,
                        KnowledgeDocument.status.in_(("ready", "degraded")),
                        _personal_knowledge_agent_visibility_predicate(
                            principal=principal,
                        ),
                        KnowledgeSegment.tenant_id == tenant_id,
                        KnowledgeSegment.scope_type == "person",
                        KnowledgeSegment.scope_id == owner_user_id,
                        KnowledgeSegment.id.in_(fetch_segment_ids),
                        _personal_knowledge_access_predicate(
                            tenant_id=tenant_id,
                            owner_user_id=owner_user_id,
                            principal=principal,
                        ),
                    )
                )
            ).all()
            entity_rank_by_segment = {segment_id: rank for rank, segment_id in enumerate(entity_segment_ids, start=1)}
            graph_rank_by_segment = {segment_id: rank for rank, segment_id in enumerate(graph_segment_ids, start=1)}
            vector_rank_by_segment = {segment_id: rank for rank, segment_id in enumerate(vector_segment_ids, start=1)}
            for row in segment_rows:
                segment, document = row[0], row[1]
                if segment.id in entity_rank_by_segment:
                    add_candidate(
                        segment=segment,
                        document=document,
                        channel="entity",
                        rank=entity_rank_by_segment[segment.id],
                        raw_score=1.0,
                    )
                if segment.id in graph_rank_by_segment:
                    add_candidate(
                        segment=segment,
                        document=document,
                        channel="graph",
                        rank=graph_rank_by_segment[segment.id],
                        raw_score=graph_segment_scores.get(segment.id, 0.0),
                        channel_metadata={"method": "ppr", "hops": graph_segment_hops.get(segment.id, 1)},
                    )
                if segment.id in vector_rank_by_segment:
                    add_candidate(
                        segment=segment,
                        document=document,
                        channel="optional_vector",
                        rank=vector_rank_by_segment[segment.id],
                        raw_score=vector_segment_scores.get(segment.id, 0.0),
                        channel_metadata=vector_segment_metadata.get(segment.id, {}),
                    )

        hits: list[KnowledgeSearchHit] = []
        ranked_entries = sorted(
            candidates.values(),
            key=lambda entry: (
                float(entry["rrf"]) + float(entry["boosts"]["heat"]) + float(entry["boosts"]["freshness"]),
                max((trace["raw_score"] for trace in entry["channels"].values()), default=0.0),
            ),
            reverse=True,
        )
        if result_limit is not None:
            ranked_entries = ranked_entries[:result_limit]
        for entry in ranked_entries:
            segment, document = entry["segment"], entry["document"]
            boosts = dict(entry["boosts"])
            final_score = float(entry["rrf"]) + float(boosts["heat"]) + float(boosts["freshness"])
            document_metadata = dict(getattr(document, "doc_metadata_json", None) or {})
            try:
                sensitivity = canonicalize_sensitivity(getattr(document, "sensitivity", None))
            except ValueError:
                # Unknown legacy sensitivity fails closed as credential-like: no
                # title, heading, source path, or segment bytes leave this layer.
                sensitivity = SensitivityLevel.PL4_CREDENTIAL
            credential_reference = (
                _credential_reference_from_metadata(document_metadata)
                if sensitivity == SensitivityLevel.PL4_CREDENTIAL
                else None
            )
            if sensitivity == SensitivityLevel.PL4_CREDENTIAL:
                title = "Credential reference"
                snippet = ""
                source_ref = credential_reference or ""
                heading_path: list[str] = []
                safe_metadata: dict[str, Any] = {}
                safe_document_metadata: dict[str, Any] = {}
            else:
                title = str(document.title or "Untitled knowledge document")
                snippet = str(segment.content or "").strip()
                source_ref = entry["source_ref"]
                heading_path = list(segment.heading_path_json or [])
                safe_metadata = {
                    "canonical_md_path": document.canonical_md_path,
                    "source_sha256": document.source_sha256,
                }
                safe_document_metadata = {
                    key: document_metadata[key]
                    for key in ("citation_count", "usage_count", "reference_count")
                    if key in document_metadata
                }
            hits.append(
                KnowledgeSearchHit(
                    document_id=document.id,
                    segment_id=segment.id,
                    title=title,
                    snippet=snippet,
                    source_ref=source_ref,
                    score=final_score,
                    heading_path=heading_path,
                    sensitivity=sensitivity.value,
                    metadata=safe_metadata,
                    score_trace={
                        "channels": dict(entry["channels"]),
                        "rrf": float(entry["rrf"]),
                        "boosts": boosts,
                        "final": final_score,
                        "optional_vector": optional_vector_trace,
                        "document_status": str(getattr(document, "status", "ready") or "ready"),
                        "document_metadata": safe_document_metadata,
                    },
                    credential_reference=credential_reference,
                )
            )
        return hits
