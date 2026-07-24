"""Permission-aware Company Knowledge retrieval and citation gateway.

The gateway is the only read path shared by API and Agent tools. It selects
active publications, re-evaluates current ResourcePermission and source ACL
facts before exposing any metadata or content, and records the decision in the
Company Knowledge event stream. Canonical artifact paths never leave this
boundary.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import desc, func, or_, select

from app.models.company_knowledge import (
    CompanyKnowledgeEvidence,
    CompanyKnowledgeProposal,
    CompanyKnowledgePublication,
    CompanyKnowledgeSource,
)
from app.models.knowledge import KnowledgeDocument, KnowledgeSegment
from app.services.company_knowledge_evidence import (
    CompanyKnowledgeEventInput,
    append_company_knowledge_event,
)
from app.services.company_knowledge_permissions import (
    CompanyKnowledgePermissionDecision,
    CompanyKnowledgePrincipal,
    CompanyKnowledgeResource,
    resolve_company_knowledge_permission,
)
from app.services.personal_knowledge_index_search import escape_like
from app.services.privacy_layer import canonicalize_sensitivity


PermissionResolver = Callable[..., Awaitable[CompanyKnowledgePermissionDecision]]
_MAX_SEARCH_RESULTS = 50
_DEFAULT_SEARCH_RESULTS = 10
_MAX_READ_CHARS = 100_000
_DEFAULT_READ_CHARS = 20_000
_MAX_DOCUMENT_LIST_RESULTS = 200
_EVIDENCE_PREFIX = "company-evidence://"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _clean_trace_id(value: str | None) -> str:
    rendered = str(value or "").strip()
    return rendered[:300] if rendered else f"company-kb:{uuid.uuid4()}"


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _source_ref(publication: CompanyKnowledgePublication, document_id: uuid.UUID, segment_id: uuid.UUID | None) -> str:
    ref = f"company-publication://{publication.id}/documents/{document_id}"
    return f"{ref}#segment={segment_id}" if segment_id else ref


def _evidence_ids(publication: CompanyKnowledgePublication) -> tuple[uuid.UUID, ...]:
    refs = tuple(str(ref) for ref in (publication.evidence_bundle_refs_json or []) if str(ref).strip())
    if not refs:
        return ()
    identifiers: list[uuid.UUID] = []
    for ref in refs:
        if not ref.startswith(_EVIDENCE_PREFIX):
            return ()
        identifier = _coerce_uuid(ref.removeprefix(_EVIDENCE_PREFIX).split("#", 1)[0])
        if identifier is None:
            return ()
        identifiers.append(identifier)
    return tuple(dict.fromkeys(identifiers))


def _bounded_snippet(content: str, query: str, *, limit: int = 480) -> str:
    text = str(content or "").strip()
    if len(text) <= limit:
        return text
    query_position = text.casefold().find(str(query or "").strip().casefold())
    if query_position < 0:
        return f"{text[: limit - 1].rstrip()}…"
    start = max(0, query_position - limit // 3)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    bounded = text[start:end].strip()
    return f"{'…' if start else ''}{bounded}{'…' if end < len(text) else ''}"


def _normalize_filters(filters: dict[str, Any] | None) -> dict[str, tuple[Any, ...]]:
    raw = _json_dict(filters)
    normalized: dict[str, tuple[Any, ...]] = {}

    namespaces = raw.get("namespaces")
    if isinstance(namespaces, list):
        values = tuple(dict.fromkeys(str(value).strip() for value in namespaces if str(value).strip()))
        if values:
            normalized["namespaces"] = values[:50]

    sensitivities = raw.get("sensitivities")
    if isinstance(sensitivities, list):
        values = tuple(
            dict.fromkeys(canonicalize_sensitivity(value).value for value in sensitivities if str(value).strip())
        )
        if values:
            normalized["sensitivities"] = values

    for key in ("publication_ids", "document_ids"):
        raw_values = raw.get(key)
        if not isinstance(raw_values, list):
            continue
        values = tuple(
            dict.fromkeys(identifier for value in raw_values if (identifier := _coerce_uuid(value)) is not None)
        )
        if values:
            normalized[key] = values[:100]
    return normalized


class _JsonResult:
    def as_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeSearchRequest:
    query: str
    filters: dict[str, Any]
    limit: int | None
    trace_id: str


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeReadRequest:
    document_id: uuid.UUID | None
    publication_id: uuid.UUID | None
    segment_ids: tuple[uuid.UUID, ...]
    max_chars: int | None
    trace_id: str


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeDocumentListRequest:
    filters: dict[str, Any]
    limit: int
    trace_id: str


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeSourceExplainRequest:
    evidence_id: uuid.UUID
    trace_id: str


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeSearchHit:
    publication_id: uuid.UUID
    document_id: uuid.UUID
    segment_id: uuid.UUID
    version: int
    title: str
    namespace: str
    snippet: str
    source_ref: str
    sensitivity: str
    score: float
    score_trace: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "result_kind": "company_knowledge_segment",
            "publication_id": str(self.publication_id),
            "document_id": str(self.document_id),
            "segment_id": str(self.segment_id),
            "version": self.version,
            "title": self.title,
            "namespace": self.namespace,
            "snippet": self.snippet,
            "source_ref": self.source_ref,
            "sensitivity": self.sensitivity,
            "score": self.score,
            "score_trace": dict(self.score_trace),
        }


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeSearchResult(_JsonResult):
    status: str
    results: tuple[CompanyKnowledgeSearchHit, ...]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "results": [result.as_dict() for result in self.results],
            "authority": {
                "schema": "hive.company_knowledge_retrieval_authority.v1",
                "scope": "company",
                "evaluation": "per_result_fresh",
                "required_actions": ["discover", "search"],
                "publication_state": "active_valid_only",
            },
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeReadSegment:
    segment_id: uuid.UUID
    position: int
    heading_path: tuple[str, ...]
    content: str
    source_ref: str
    sensitivity: str
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "result_kind": "company_knowledge_segment",
            "segment_id": str(self.segment_id),
            "position": self.position,
            "heading_path": list(self.heading_path),
            "content": self.content,
            "source_ref": self.source_ref,
            "sensitivity": self.sensitivity,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeReadResult(_JsonResult):
    status: str
    publication_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    version: int | None = None
    title: str | None = None
    namespace: str | None = None
    sensitivity: str | None = None
    segments: tuple[CompanyKnowledgeReadSegment, ...] = ()
    citations: tuple[str, ...] = ()
    truncated: bool = False
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "segments": [segment.as_dict() for segment in self.segments],
            "citations": list(self.citations),
            "truncated": self.truncated,
            "authority": {
                "schema": "hive.company_knowledge_retrieval_authority.v1",
                "scope": "company",
                "evaluation": "fresh_read_and_cite",
                "required_actions": ["read", "cite"],
                "publication_state": "active_valid_only",
            },
            "warnings": list(self.warnings),
        }
        if self.status == "ok":
            payload.update(
                {
                    "result_kind": "company_knowledge_document",
                    "publication_id": str(self.publication_id),
                    "document_id": str(self.document_id),
                    "version": self.version,
                    "title": self.title,
                    "namespace": self.namespace,
                    "sensitivity": self.sensitivity,
                    "source_ref": (f"company-publication://{self.publication_id}/documents/{self.document_id}"),
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeDocumentSummary:
    publication_id: uuid.UUID
    document_id: uuid.UUID
    title: str
    namespace: str
    sensitivity: str
    version: int
    valid_from: datetime
    valid_until: datetime | None
    source_ref: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "publication_id": str(self.publication_id),
            "document_id": str(self.document_id),
            "title": self.title,
            "namespace": self.namespace,
            "sensitivity": self.sensitivity,
            "version": self.version,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeDocumentListResult(_JsonResult):
    status: str
    documents: tuple[CompanyKnowledgeDocumentSummary, ...]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "documents": [document.as_dict() for document in self.documents],
            "authority": {
                "schema": "hive.company_knowledge_retrieval_authority.v1",
                "scope": "company",
                "evaluation": "per_result_fresh",
                "required_actions": ["discover", "search"],
                "publication_state": "active_valid_only",
            },
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeSourceExplainResult(_JsonResult):
    status: str
    payload: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "authority": {
                "schema": "hive.company_knowledge_retrieval_authority.v1",
                "scope": "company",
                "evaluation": "fresh_cite",
                "required_actions": ["cite"],
                "publication_state": "active_valid_only",
            },
            "warnings": list(self.warnings),
        }
        if self.status == "ok" and self.payload is not None:
            result.update(self.payload)
        return result


@dataclass(frozen=True, slots=True)
class _PublicationBundle:
    publication: CompanyKnowledgePublication
    document: KnowledgeDocument
    proposal: CompanyKnowledgeProposal
    source: CompanyKnowledgeSource


class CompanyKnowledgeGateway:
    """Single Company Knowledge read boundary for tools and authenticated API."""

    def __init__(self, *, permission_resolver: PermissionResolver = resolve_company_knowledge_permission) -> None:
        self._permission_resolver = permission_resolver

    @staticmethod
    def _active_publication_predicates(*, tenant_id: uuid.UUID, now: datetime) -> tuple[Any, ...]:
        return (
            CompanyKnowledgePublication.tenant_id == tenant_id,
            CompanyKnowledgePublication.status == "active",
            CompanyKnowledgePublication.valid_from <= now,
            or_(
                CompanyKnowledgePublication.valid_until.is_(None),
                CompanyKnowledgePublication.valid_until > now,
            ),
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.scope_type == "company",
            KnowledgeDocument.scope_id == tenant_id,
            KnowledgeDocument.status.in_(("ready", "degraded")),
            KnowledgeDocument.agent_searchable.is_(True),
            CompanyKnowledgeProposal.tenant_id == tenant_id,
            CompanyKnowledgeSource.tenant_id == tenant_id,
            CompanyKnowledgeSource.status == "ingested",
        )

    @staticmethod
    def _apply_filters(statement: Any, filters: dict[str, tuple[Any, ...]]) -> Any:
        if namespaces := filters.get("namespaces"):
            statement = statement.where(CompanyKnowledgePublication.namespace.in_(namespaces))
        if sensitivities := filters.get("sensitivities"):
            statement = statement.where(CompanyKnowledgePublication.sensitivity.in_(sensitivities))
        if publication_ids := filters.get("publication_ids"):
            statement = statement.where(CompanyKnowledgePublication.id.in_(publication_ids))
        if document_ids := filters.get("document_ids"):
            statement = statement.where(CompanyKnowledgePublication.document_id.in_(document_ids))
        return statement

    @staticmethod
    def _bundle_from_row(row: Any) -> _PublicationBundle:
        return _PublicationBundle(
            publication=row[0],
            document=row[1],
            proposal=row[2],
            source=row[3],
        )

    async def _load_evidence(
        self,
        session: Any,
        bundles: tuple[_PublicationBundle, ...],
    ) -> dict[uuid.UUID, CompanyKnowledgeEvidence]:
        identifiers = {evidence_id for bundle in bundles for evidence_id in _evidence_ids(bundle.publication)}
        if not identifiers:
            return {}
        rows = (
            (
                await session.execute(
                    select(CompanyKnowledgeEvidence).where(
                        CompanyKnowledgeEvidence.tenant_id == bundles[0].publication.tenant_id,
                        CompanyKnowledgeEvidence.id.in_(identifiers),
                    )
                )
            )
            .scalars()
            .all()
        )
        return {row.id: row for row in rows}

    @staticmethod
    def _evidence_complete(
        bundle: _PublicationBundle,
        evidence_by_id: dict[uuid.UUID, CompanyKnowledgeEvidence],
    ) -> bool:
        evidence_ids = _evidence_ids(bundle.publication)
        if not evidence_ids:
            return False
        evidence_rows = [evidence_by_id.get(identifier) for identifier in evidence_ids]
        return bool(
            bundle.proposal.source_coverage_json.get("complete") is True
            and bundle.document.source_sha256 == bundle.publication.content_hash
            and bundle.document.canonical_md_sha256 == bundle.publication.content_hash
            and all(
                row is not None
                and row.status == "accepted"
                and row.source_id == bundle.source.id
                and row.source_acl_snapshot_hash == bundle.source.source_acl_snapshot_hash
                for row in evidence_rows
            )
        )

    def _resource(
        self,
        bundle: _PublicationBundle,
        *,
        evidence_by_id: dict[uuid.UUID, CompanyKnowledgeEvidence],
        now: datetime,
    ) -> CompanyKnowledgeResource:
        publication = bundle.publication
        valid_from = publication.valid_from
        valid_until = publication.valid_until
        if valid_from.tzinfo is None:
            valid_from = valid_from.replace(tzinfo=timezone.utc)
        if valid_until is not None and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        return CompanyKnowledgeResource(
            tenant_id=publication.tenant_id,
            resource_type="company_knowledge_document",
            resource_id=publication.document_id,
            resource_key=f"document:{publication.document_id}",
            namespace=publication.namespace,
            sensitivity=publication.sensitivity,
            source_acl_snapshot_hash=bundle.source.source_acl_snapshot_hash,
            source_acl=dict(bundle.source.source_acl_snapshot_json or {}),
            evidence_access_complete=self._evidence_complete(bundle, evidence_by_id),
            publication_status=publication.status,
            validity_active=valid_from <= now and (valid_until is None or valid_until > now),
        )

    async def _resolve(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        bundle: _PublicationBundle,
        evidence_by_id: dict[uuid.UUID, CompanyKnowledgeEvidence],
        action: str,
        trace_id: str,
        ordinal: int,
        now: datetime,
    ) -> CompanyKnowledgePermissionDecision:
        resource = self._resource(bundle, evidence_by_id=evidence_by_id, now=now)
        decision = await self._permission_resolver(
            session,
            principal=principal,
            resource=resource,
            action=action,
            now=now,
        )
        event_type = (
            "company_knowledge.permission_allowed" if decision.allowed else "company_knowledge.permission_denied"
        )
        await self._append_event(
            session,
            principal=principal,
            event_type=event_type,
            resource_type="publication",
            resource_id=bundle.publication.id,
            resource_version=bundle.publication.version,
            source_refs=((_source_ref(bundle.publication, bundle.document.id, None),) if decision.allowed else ()),
            source_hash=bundle.publication.content_hash,
            policy_snapshot=decision.evidence(),
            trace_id=trace_id,
            idempotency_parts=(
                "permission",
                action,
                str(bundle.publication.id),
                str(ordinal),
                "allowed" if decision.allowed else str(decision.deny_reason_code or "denied"),
            ),
            outcome="allowed" if decision.allowed else "denied",
            payload={
                "requested_action": action,
                "deny_reason_code": decision.deny_reason_code,
                "retryable": decision.retryable,
            },
        )
        return decision

    @staticmethod
    async def _append_event(
        session: Any,
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
        idempotency_parts: tuple[str, ...],
        outcome: str,
        payload: dict[str, Any],
    ) -> None:
        identity = ":".join(idempotency_parts)
        await append_company_knowledge_event(
            session,
            event_input=CompanyKnowledgeEventInput(
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
                idempotency_key=f"retrieval:{_sha256([trace_id, identity])}",
                outcome=outcome,
                payload=payload,
                occurred_at=_utcnow(),
            ),
        )

    async def search(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: CompanyKnowledgeSearchRequest,
    ) -> CompanyKnowledgeSearchResult:
        query = " ".join(str(request.query or "").split())
        if not query:
            raise ValueError("company_knowledge_search_query_required")
        limit = _DEFAULT_SEARCH_RESULTS if request.limit is None else int(request.limit)
        if limit < 1 or limit > _MAX_SEARCH_RESULTS:
            raise ValueError("company_knowledge_search_limit_out_of_range")
        filters = _normalize_filters(request.filters)
        trace_id = _clean_trace_id(request.trace_id)
        now = _utcnow()
        ts_query = func.plainto_tsquery("simple", query)
        score = func.coalesce(func.ts_rank_cd(KnowledgeSegment.tsv, ts_query), 0.0).label("score")
        like_query = f"%{escape_like(query)}%"
        statement = (
            select(
                CompanyKnowledgePublication,
                KnowledgeDocument,
                CompanyKnowledgeProposal,
                CompanyKnowledgeSource,
                KnowledgeSegment,
                score,
            )
            .join(KnowledgeDocument, KnowledgeDocument.id == CompanyKnowledgePublication.document_id)
            .join(CompanyKnowledgeProposal, CompanyKnowledgeProposal.id == CompanyKnowledgePublication.proposal_id)
            .join(CompanyKnowledgeSource, CompanyKnowledgeSource.id == CompanyKnowledgeProposal.source_id)
            .join(KnowledgeSegment, KnowledgeSegment.document_id == KnowledgeDocument.id)
            .where(
                *self._active_publication_predicates(tenant_id=principal.tenant_id, now=now),
                KnowledgeSegment.tenant_id == principal.tenant_id,
                KnowledgeSegment.scope_type == "company",
                KnowledgeSegment.scope_id == principal.tenant_id,
                or_(
                    KnowledgeSegment.tsv.op("@@")(ts_query),
                    KnowledgeSegment.content.ilike(like_query, escape="\\"),
                    KnowledgeDocument.title.ilike(like_query, escape="\\"),
                ),
            )
            .order_by(
                desc(score),
                CompanyKnowledgePublication.published_at.desc(),
                KnowledgeSegment.position.asc(),
            )
        )
        statement = self._apply_filters(statement, filters)
        rows = (await session.execute(statement)).all()
        bundles = tuple(self._bundle_from_row(row) for row in rows)
        evidence_by_id = await self._load_evidence(session, bundles)

        results: list[CompanyKnowledgeSearchHit] = []
        authorized_publications: set[uuid.UUID] = set()
        decisions: dict[tuple[uuid.UUID, str], CompanyKnowledgePermissionDecision] = {}
        for ordinal, row in enumerate(rows):
            bundle = self._bundle_from_row(row)
            publication_id = bundle.publication.id
            discover_key = (publication_id, "discover")
            if discover_key not in decisions:
                decisions[discover_key] = await self._resolve(
                    session,
                    principal=principal,
                    bundle=bundle,
                    evidence_by_id=evidence_by_id,
                    action="discover",
                    trace_id=trace_id,
                    ordinal=ordinal,
                    now=now,
                )
            if not decisions[discover_key].allowed:
                continue
            search_key = (publication_id, "search")
            if search_key not in decisions:
                decisions[search_key] = await self._resolve(
                    session,
                    principal=principal,
                    bundle=bundle,
                    evidence_by_id=evidence_by_id,
                    action="search",
                    trace_id=trace_id,
                    ordinal=ordinal,
                    now=now,
                )
            if not decisions[search_key].allowed:
                continue
            segment: KnowledgeSegment = row[4]
            numeric_score = float(row[5] or 0.0)
            results.append(
                CompanyKnowledgeSearchHit(
                    publication_id=publication_id,
                    document_id=bundle.document.id,
                    segment_id=segment.id,
                    version=bundle.publication.version,
                    title=bundle.document.title,
                    namespace=bundle.publication.namespace,
                    snippet=_bounded_snippet(segment.content, query),
                    source_ref=_source_ref(bundle.publication, bundle.document.id, segment.id),
                    sensitivity=bundle.publication.sensitivity,
                    score=numeric_score,
                    score_trace={
                        "channel": "postgres_fts",
                        "fts_score": numeric_score,
                        "publication_status": "active",
                        "validity": "current",
                    },
                )
            )
            authorized_publications.add(publication_id)
            if len(results) >= limit:
                break

        await self._append_event(
            session,
            principal=principal,
            event_type="company_knowledge.searched",
            resource_type="company_knowledge_scope",
            resource_id=principal.tenant_id,
            resource_version=None,
            source_refs=tuple(
                _source_ref(bundle.publication, bundle.document.id, None)
                for bundle in bundles
                if bundle.publication.id in authorized_publications
            ),
            source_hash=None,
            policy_snapshot={
                "schema": "hive.company_knowledge_search_policy.v1",
                "required_actions": ["discover", "search"],
                "active_valid_only": True,
                "per_result_fresh": True,
            },
            trace_id=trace_id,
            idempotency_parts=("search", _sha256(query), _sha256(filters)),
            outcome="ok" if results else "empty",
            payload={
                "query_sha256": _sha256(query),
                "result_count": len(results),
                "filters_sha256": _sha256(filters),
            },
        )
        return CompanyKnowledgeSearchResult(
            status="ok" if results else "empty",
            results=tuple(results),
        )

    async def _select_bundle(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID | None,
        publication_id: uuid.UUID | None,
        now: datetime,
    ) -> _PublicationBundle | None:
        if document_id is None and publication_id is None:
            raise ValueError("company_knowledge_document_or_publication_id_required")
        statement = (
            select(
                CompanyKnowledgePublication,
                KnowledgeDocument,
                CompanyKnowledgeProposal,
                CompanyKnowledgeSource,
            )
            .join(KnowledgeDocument, KnowledgeDocument.id == CompanyKnowledgePublication.document_id)
            .join(CompanyKnowledgeProposal, CompanyKnowledgeProposal.id == CompanyKnowledgePublication.proposal_id)
            .join(CompanyKnowledgeSource, CompanyKnowledgeSource.id == CompanyKnowledgeProposal.source_id)
            .where(*self._active_publication_predicates(tenant_id=tenant_id, now=now))
            .limit(1)
        )
        if document_id is not None:
            statement = statement.where(CompanyKnowledgePublication.document_id == document_id)
        if publication_id is not None:
            statement = statement.where(CompanyKnowledgePublication.id == publication_id)
        row = (await session.execute(statement)).first()
        return self._bundle_from_row(row) if row is not None else None

    async def _record_not_found_read(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: CompanyKnowledgeReadRequest,
        trace_id: str,
    ) -> None:
        requested_id = request.publication_id or request.document_id
        await self._append_event(
            session,
            principal=principal,
            event_type="company_knowledge.read",
            resource_type="publication" if request.publication_id else "knowledge_document",
            resource_id=requested_id,
            resource_version=None,
            source_refs=(),
            source_hash=None,
            policy_snapshot={
                "schema": "hive.company_knowledge_retrieval_authority.v1",
                "allowed": False,
                "redaction_policy": "withhold_resource_existence",
            },
            trace_id=trace_id,
            idempotency_parts=("read", str(requested_id), "not_found_or_denied"),
            outcome="not_found_or_denied",
            payload={"result_count": 0},
        )

    async def read(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: CompanyKnowledgeReadRequest,
    ) -> CompanyKnowledgeReadResult:
        trace_id = _clean_trace_id(request.trace_id)
        max_chars = _DEFAULT_READ_CHARS if request.max_chars is None else int(request.max_chars)
        if max_chars < 1 or max_chars > _MAX_READ_CHARS:
            raise ValueError("company_knowledge_read_max_chars_out_of_range")
        now = _utcnow()
        bundle = await self._select_bundle(
            session,
            tenant_id=principal.tenant_id,
            document_id=request.document_id,
            publication_id=request.publication_id,
            now=now,
        )
        if bundle is None:
            await self._record_not_found_read(session, principal=principal, request=request, trace_id=trace_id)
            return CompanyKnowledgeReadResult(status="not_found_or_denied")
        evidence_by_id = await self._load_evidence(session, (bundle,))
        for ordinal, action in enumerate(("read", "cite")):
            decision = await self._resolve(
                session,
                principal=principal,
                bundle=bundle,
                evidence_by_id=evidence_by_id,
                action=action,
                trace_id=trace_id,
                ordinal=ordinal,
                now=now,
            )
            if not decision.allowed:
                await self._record_not_found_read(session, principal=principal, request=request, trace_id=trace_id)
                return CompanyKnowledgeReadResult(status="not_found_or_denied")
        if bundle.publication.sensitivity == "PL4_credential":
            await self._record_not_found_read(session, principal=principal, request=request, trace_id=trace_id)
            return CompanyKnowledgeReadResult(
                status="not_found_or_denied",
                warnings=("credential_content_not_exposed",),
            )

        statement = (
            select(KnowledgeSegment)
            .where(
                KnowledgeSegment.tenant_id == principal.tenant_id,
                KnowledgeSegment.document_id == bundle.document.id,
                KnowledgeSegment.scope_type == "company",
                KnowledgeSegment.scope_id == principal.tenant_id,
            )
            .order_by(KnowledgeSegment.position, KnowledgeSegment.id)
        )
        segment_ids = tuple(dict.fromkeys(request.segment_ids))
        if segment_ids:
            statement = statement.where(KnowledgeSegment.id.in_(segment_ids))
        segments = (await session.execute(statement)).scalars().all()
        rendered: list[CompanyKnowledgeReadSegment] = []
        remaining = max_chars
        truncated = False
        for index, segment in enumerate(segments):
            if remaining <= 0:
                truncated = True
                break
            content = str(segment.content or "")
            bounded = content[:remaining]
            segment_truncated = len(bounded) < len(content)
            rendered.append(
                CompanyKnowledgeReadSegment(
                    segment_id=segment.id,
                    position=segment.position,
                    heading_path=tuple(str(value) for value in (segment.heading_path_json or [])),
                    content=bounded,
                    source_ref=_source_ref(bundle.publication, bundle.document.id, segment.id),
                    sensitivity=bundle.publication.sensitivity,
                    truncated=segment_truncated,
                )
            )
            remaining -= len(bounded)
            if segment_truncated or (remaining <= 0 and index < len(segments) - 1):
                truncated = True
                break

        citations = tuple(str(ref) for ref in (bundle.publication.evidence_bundle_refs_json or []))
        outcome = "ok" if rendered else "empty"
        await self._append_event(
            session,
            principal=principal,
            event_type="company_knowledge.read",
            resource_type="publication",
            resource_id=bundle.publication.id,
            resource_version=bundle.publication.version,
            source_refs=(
                _source_ref(bundle.publication, bundle.document.id, None),
                *citations,
            ),
            source_hash=bundle.publication.content_hash,
            policy_snapshot={
                "schema": "hive.company_knowledge_read_policy.v1",
                "required_actions": ["read", "cite"],
                "active_valid_only": True,
                "fresh_permission": True,
            },
            trace_id=trace_id,
            idempotency_parts=(
                "read",
                str(bundle.publication.id),
                _sha256([str(value) for value in segment_ids]),
                str(max_chars),
            ),
            outcome=outcome,
            payload={
                "segment_count": len(rendered),
                "truncated": truncated,
                "citation_count": len(citations),
            },
        )
        if not rendered:
            return CompanyKnowledgeReadResult(status="empty")
        return CompanyKnowledgeReadResult(
            status="ok",
            publication_id=bundle.publication.id,
            document_id=bundle.document.id,
            version=bundle.publication.version,
            title=bundle.document.title,
            namespace=bundle.publication.namespace,
            sensitivity=bundle.publication.sensitivity,
            segments=tuple(rendered),
            citations=citations,
            truncated=truncated,
        )

    async def list_documents(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: CompanyKnowledgeDocumentListRequest,
    ) -> CompanyKnowledgeDocumentListResult:
        limit = int(request.limit)
        if limit < 1 or limit > _MAX_DOCUMENT_LIST_RESULTS:
            raise ValueError("company_knowledge_document_list_limit_out_of_range")
        filters = _normalize_filters(request.filters)
        trace_id = _clean_trace_id(request.trace_id)
        now = _utcnow()
        statement = (
            select(
                CompanyKnowledgePublication,
                KnowledgeDocument,
                CompanyKnowledgeProposal,
                CompanyKnowledgeSource,
            )
            .join(KnowledgeDocument, KnowledgeDocument.id == CompanyKnowledgePublication.document_id)
            .join(CompanyKnowledgeProposal, CompanyKnowledgeProposal.id == CompanyKnowledgePublication.proposal_id)
            .join(CompanyKnowledgeSource, CompanyKnowledgeSource.id == CompanyKnowledgeProposal.source_id)
            .where(*self._active_publication_predicates(tenant_id=principal.tenant_id, now=now))
            .order_by(CompanyKnowledgePublication.published_at.desc(), CompanyKnowledgePublication.id)
        )
        rows = (await session.execute(self._apply_filters(statement, filters))).all()
        bundles = tuple(self._bundle_from_row(row) for row in rows)
        evidence_by_id = await self._load_evidence(session, bundles)
        documents: list[CompanyKnowledgeDocumentSummary] = []
        for ordinal, bundle in enumerate(bundles):
            allowed = True
            for action_offset, action in enumerate(("discover", "search")):
                decision = await self._resolve(
                    session,
                    principal=principal,
                    bundle=bundle,
                    evidence_by_id=evidence_by_id,
                    action=action,
                    trace_id=trace_id,
                    ordinal=ordinal * 2 + action_offset,
                    now=now,
                )
                if not decision.allowed:
                    allowed = False
                    break
            if not allowed:
                continue
            documents.append(
                CompanyKnowledgeDocumentSummary(
                    publication_id=bundle.publication.id,
                    document_id=bundle.document.id,
                    title=bundle.document.title,
                    namespace=bundle.publication.namespace,
                    sensitivity=bundle.publication.sensitivity,
                    version=bundle.publication.version,
                    valid_from=bundle.publication.valid_from,
                    valid_until=bundle.publication.valid_until,
                    source_ref=_source_ref(bundle.publication, bundle.document.id, None),
                )
            )
            if len(documents) >= limit:
                break
        return CompanyKnowledgeDocumentListResult(
            status="ok" if documents else "empty",
            documents=tuple(documents),
        )

    async def explain_source(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: CompanyKnowledgeSourceExplainRequest,
    ) -> CompanyKnowledgeSourceExplainResult:
        trace_id = _clean_trace_id(request.trace_id)
        now = _utcnow()
        evidence = (
            await session.execute(
                select(CompanyKnowledgeEvidence).where(
                    CompanyKnowledgeEvidence.tenant_id == principal.tenant_id,
                    CompanyKnowledgeEvidence.id == request.evidence_id,
                    CompanyKnowledgeEvidence.status == "accepted",
                )
            )
        ).scalar_one_or_none()
        if evidence is None:
            return CompanyKnowledgeSourceExplainResult(status="not_found_or_denied")
        row = (
            await session.execute(
                select(
                    CompanyKnowledgePublication,
                    KnowledgeDocument,
                    CompanyKnowledgeProposal,
                    CompanyKnowledgeSource,
                )
                .join(KnowledgeDocument, KnowledgeDocument.id == CompanyKnowledgePublication.document_id)
                .join(CompanyKnowledgeProposal, CompanyKnowledgeProposal.id == CompanyKnowledgePublication.proposal_id)
                .join(CompanyKnowledgeSource, CompanyKnowledgeSource.id == CompanyKnowledgeProposal.source_id)
                .where(
                    *self._active_publication_predicates(tenant_id=principal.tenant_id, now=now),
                    CompanyKnowledgeProposal.source_id == evidence.source_id,
                )
                .order_by(CompanyKnowledgePublication.published_at.desc())
            )
        ).first()
        if row is None:
            return CompanyKnowledgeSourceExplainResult(status="not_found_or_denied")
        bundle = self._bundle_from_row(row)
        if request.evidence_id not in _evidence_ids(bundle.publication):
            return CompanyKnowledgeSourceExplainResult(status="not_found_or_denied")
        evidence_by_id = await self._load_evidence(session, (bundle,))
        if evidence.id not in evidence_by_id:
            return CompanyKnowledgeSourceExplainResult(status="not_found_or_denied")
        decision = await self._resolve(
            session,
            principal=principal,
            bundle=bundle,
            evidence_by_id=evidence_by_id,
            action="cite",
            trace_id=trace_id,
            ordinal=0,
            now=now,
        )
        if not decision.allowed:
            return CompanyKnowledgeSourceExplainResult(status="not_found_or_denied")
        source_ref = f"{_EVIDENCE_PREFIX}{evidence.id}"
        payload = {
            "result_kind": "company_knowledge_evidence",
            "evidence_id": str(evidence.id),
            "source_ref": source_ref,
            "publication_id": str(bundle.publication.id),
            "document_id": str(bundle.document.id),
            "evidence_kind": evidence.evidence_kind,
            "source_contract_id": str(evidence.source_contract_id),
            "source_contract_version": evidence.source_contract_version,
            "source_revision": evidence.source_revision,
            "content_hash": evidence.content_hash,
            "occurred_at": evidence.occurred_at.isoformat() if evidence.occurred_at else None,
            "effective_from": evidence.effective_from.isoformat() if evidence.effective_from else None,
            "effective_until": evidence.effective_until.isoformat() if evidence.effective_until else None,
            "observed_at": evidence.observed_at.isoformat(),
            "coverage": dict(evidence.coverage_ledger_json or {}),
            "ingestion_receipt_ref": evidence.ingestion_receipt_ref,
            "sensitivity": bundle.publication.sensitivity,
        }
        await self._append_event(
            session,
            principal=principal,
            event_type="company_knowledge.read",
            resource_type="evidence",
            resource_id=evidence.id,
            resource_version=evidence.source_contract_version,
            source_refs=(source_ref, _source_ref(bundle.publication, bundle.document.id, None)),
            source_hash=evidence.content_hash,
            policy_snapshot=decision.evidence(),
            trace_id=trace_id,
            idempotency_parts=("explain_source", str(evidence.id)),
            outcome="ok",
            payload={"publication_id": str(bundle.publication.id), "coverage_complete": True},
        )
        return CompanyKnowledgeSourceExplainResult(status="ok", payload=payload)


__all__ = [
    "CompanyKnowledgeDocumentListRequest",
    "CompanyKnowledgeDocumentListResult",
    "CompanyKnowledgeGateway",
    "CompanyKnowledgeReadRequest",
    "CompanyKnowledgeReadResult",
    "CompanyKnowledgeSearchRequest",
    "CompanyKnowledgeSearchResult",
    "CompanyKnowledgeSourceExplainRequest",
    "CompanyKnowledgeSourceExplainResult",
]
