"""Durable import-job claiming contracts for Personal Knowledge."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, false, or_, select

from app.models.knowledge import KnowledgeIndexJob

DEFAULT_IMPORT_JOB_MAX_ATTEMPTS = 5


def build_personal_knowledge_job_claim_statement(
    *,
    tenant_id: uuid.UUID | None,
    owner_user_id: uuid.UUID | None,
    statuses: tuple[str, ...],
    queued_before: datetime | None,
    running_before: datetime | None,
    max_attempts: int,
    limit: int,
):
    """Build the sole SKIP LOCKED claim query used by API and fleet workers.

    Workers select queued jobs and stale-running leases only — never failed
    jobs (a failed job re-enters queued exclusively through the explicit
    retry CAS). Queued rows require attempts left; stale-running rows are
    admitted at any attempt count so a crash at the final attempt can be
    terminalized instead of leaking a permanently running row.
    """

    clean_statuses = tuple(
        status for status in (str(item or "").strip().lower() for item in statuses) if status in {"queued", "running"}
    )
    status_predicates = []
    if "queued" in clean_statuses:
        predicate = and_(
            KnowledgeIndexJob.status == "queued",
            KnowledgeIndexJob.attempt_count < max(1, int(max_attempts or DEFAULT_IMPORT_JOB_MAX_ATTEMPTS)),
        )
        if queued_before is not None:
            predicate = and_(predicate, KnowledgeIndexJob.updated_at <= queued_before)
        status_predicates.append(predicate)
    if "running" in clean_statuses:
        predicate = KnowledgeIndexJob.status == "running"
        if running_before is not None:
            predicate = and_(predicate, KnowledgeIndexJob.updated_at <= running_before)
        status_predicates.append(predicate)
    if not status_predicates:
        status_predicates.append(false())

    statement = select(KnowledgeIndexJob).where(
        KnowledgeIndexJob.scope_type == "person",
        or_(*status_predicates),
    )
    if tenant_id is not None:
        statement = statement.where(KnowledgeIndexJob.tenant_id == tenant_id)
    if owner_user_id is not None:
        statement = statement.where(KnowledgeIndexJob.scope_id == owner_user_id)
    return (
        statement.order_by(KnowledgeIndexJob.updated_at.asc(), KnowledgeIndexJob.created_at.asc())
        .limit(max(1, int(limit or 10)))
        .with_for_update(skip_locked=True)
    )
