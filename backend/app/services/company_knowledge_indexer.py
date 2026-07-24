"""Durable Company Knowledge outbox consumer for rebuildable search state."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update

from app.database import tenant_scoped_session
from app.models.company_knowledge import CompanyKnowledgeOutbox
from app.models.company_ontology import CompanyOntologyRelease
from app.models.knowledge import KnowledgeSegment
from app.services.company_ontology_engine import OntologyEnginePlugin, ReferenceOntologyEngine


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeIndexSummary:
    claimed: int
    completed: int
    failed: int


class CompanyKnowledgeIndexer:
    """Claims projection work and records terminal or retryable outcomes."""

    def __init__(self, *, ontology_engine: OntologyEnginePlugin | None = None) -> None:
        self._ontology_engine = ontology_engine or ReferenceOntologyEngine()

    async def discover_pending_tenants(
        self,
        session: Any,
        *,
        limit: int = 100,
    ) -> tuple[uuid.UUID, ...]:
        """Discover tenants with due projection work under an audited bypass."""

        if limit < 1:
            raise ValueError("company_knowledge_index_tenant_limit_must_be_positive")
        now = _utcnow()
        rows = (
            (
                await session.execute(
                    select(CompanyKnowledgeOutbox.tenant_id)
                    .where(
                        CompanyKnowledgeOutbox.status.in_(("pending", "failed", "processing")),
                        CompanyKnowledgeOutbox.available_at <= now,
                        (
                            (CompanyKnowledgeOutbox.claim_expires_at.is_(None))
                            | (CompanyKnowledgeOutbox.claim_expires_at <= now)
                        ),
                    )
                    .distinct()
                    .order_by(CompanyKnowledgeOutbox.tenant_id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return tuple(uuid.UUID(str(value)) for value in rows)

    async def process_pending(
        self,
        *,
        tenant_id: uuid.UUID,
        session_factory: Any,
        limit: int = 50,
    ) -> CompanyKnowledgeIndexSummary:
        if limit < 1:
            raise ValueError("company_knowledge_index_limit_must_be_positive")
        completed = 0
        failed = 0
        async with tenant_scoped_session(
            tenant_id,
            session_factory=session_factory,
            require_tenant=True,
            source="company_knowledge_indexer",
        ) as session:
            now = _utcnow()
            rows = (
                (
                    await session.execute(
                        select(CompanyKnowledgeOutbox)
                        .where(
                            CompanyKnowledgeOutbox.tenant_id == tenant_id,
                            CompanyKnowledgeOutbox.status.in_(("pending", "failed", "processing")),
                            CompanyKnowledgeOutbox.available_at <= now,
                            (
                                (CompanyKnowledgeOutbox.claim_expires_at.is_(None))
                                | (CompanyKnowledgeOutbox.claim_expires_at <= now)
                            ),
                        )
                        .order_by(CompanyKnowledgeOutbox.created_at, CompanyKnowledgeOutbox.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for outbox in rows:
                token = uuid.uuid4()
                outbox.status = "processing"
                outbox.claim_token = token
                outbox.claim_expires_at = now + timedelta(minutes=2)
                outbox.attempt_count += 1
                try:
                    payload = dict(outbox.payload_json or {})
                    operation = str(payload.get("operation") or "")
                    if operation == "index_document":
                        document_id = uuid.UUID(str(payload["document_id"]))
                        await session.execute(
                            update(KnowledgeSegment)
                            .where(
                                KnowledgeSegment.tenant_id == tenant_id,
                                KnowledgeSegment.document_id == document_id,
                                KnowledgeSegment.scope_type == "company",
                            )
                            .values(tsv=func.to_tsvector("simple", KnowledgeSegment.content))
                        )
                    elif operation == "tombstone_publication":
                        # Publication status remains the query-time authority.
                        # The document can still back a later immutable restore.
                        pass
                    elif operation == "project_ontology_release":
                        release_id = uuid.UUID(str(payload["release_id"]))
                        release_hash = str(payload["release_hash"])
                        release = (
                            await session.execute(
                                select(CompanyOntologyRelease).where(
                                    CompanyOntologyRelease.id == release_id,
                                    CompanyOntologyRelease.tenant_id == tenant_id,
                                    CompanyOntologyRelease.release_hash == release_hash,
                                    CompanyOntologyRelease.status == "active",
                                )
                            )
                        ).scalar_one_or_none()
                        if release is None:
                            raise ValueError("company_ontology_active_release_projection_source_unavailable")
                        receipt = await self._ontology_engine.rebuild_projection(
                            {
                                "release_id": release.id,
                                "release_hash": release.release_hash,
                                "namespace": release.namespace,
                            }
                        )
                        if receipt.get("status") != "rebuilt":
                            raise ValueError("company_ontology_projection_rebuild_failed")
                    elif operation == "tombstone_ontology_release":
                        release_id = uuid.UUID(str(payload["release_id"]))
                        release_hash = str(payload["release_hash"])
                        release = (
                            await session.execute(
                                select(CompanyOntologyRelease).where(
                                    CompanyOntologyRelease.id == release_id,
                                    CompanyOntologyRelease.tenant_id == tenant_id,
                                    CompanyOntologyRelease.release_hash == release_hash,
                                )
                            )
                        ).scalar_one_or_none()
                        if release is None or release.status == "active":
                            raise ValueError("company_ontology_tombstone_source_not_terminal")
                        # Active release rows are query-time authority. A
                        # provider may delete its derived projection here; the
                        # reference engine queries immutable release rows.
                    else:
                        raise ValueError("unsupported_company_knowledge_projection_operation")
                    outbox.status = "completed"
                    outbox.processed_at = _utcnow()
                    outbox.claim_token = None
                    outbox.claim_expires_at = None
                    outbox.last_error = None
                    completed += 1
                except Exception as exc:
                    outbox.status = "failed"
                    outbox.available_at = _utcnow() + timedelta(seconds=min(300, 2**outbox.attempt_count))
                    outbox.claim_token = None
                    outbox.claim_expires_at = None
                    outbox.last_error = str(exc)[:4000]
                    failed += 1
        return CompanyKnowledgeIndexSummary(
            claimed=completed + failed,
            completed=completed,
            failed=failed,
        )


__all__ = ["CompanyKnowledgeIndexSummary", "CompanyKnowledgeIndexer"]
