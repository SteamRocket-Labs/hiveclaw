"""Durable Company Knowledge outbox consumer for rebuildable search state."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update

from app.database import tenant_scoped_session
from app.models.company_knowledge import CompanyKnowledgeOutbox
from app.models.knowledge import KnowledgeSegment


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeIndexSummary:
    claimed: int
    completed: int
    failed: int


class CompanyKnowledgeIndexer:
    """Claims projection work and records terminal or retryable outcomes."""

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
                    document_id = uuid.UUID(str(payload["document_id"]))
                    operation = str(payload.get("operation") or "")
                    if operation == "index_document":
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
