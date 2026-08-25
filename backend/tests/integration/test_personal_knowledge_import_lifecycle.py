"""Real-PostgreSQL two-session regressions for the Personal KB import lifecycle.

RC-01 root cause: the import worker claimed a job (status=running, attempt+1)
with a flush inside one long transaction and performed the whole conversion /
indexing work before the context committed. Under READ COMMITTED every
concurrent reader saw the stale ``queued / attempt 0`` for the entire
conversion. These tests prove the durable two-phase claim: running+attempt is
COMMITTED before long work starts, terminal state commits after, and SKIP
LOCKED keeps a second worker off the same job.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.database import Base
from app.models.knowledge import KnowledgeDocument, KnowledgeIndexJob, KnowledgeSegment
from app.models.tenant import Tenant
from app.models.user import User
from app.services.personal_knowledge_service import PersonalKnowledgeService

MARKER_EN = "WEEKEND-RC-20260825-PKB-EN-MARKER"
MARKER_ZH = "周末RC20260825个人知识唯一标记"


async def _seed_owner(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as session:
        session.add(Tenant(id=tenant_id, name="T", slug=f"pkb-{suffix}"))
        await session.flush()
        session.add(
            User(
                id=owner_id,
                tenant_id=tenant_id,
                username=f"pkb-owner-{suffix}",
                email=f"pkb-owner-{suffix}@example.com",
                password_hash="not-a-real-password",
                display_name="PKB Owner",
                role="member",
                is_active=True,
            )
        )
        await session.commit()
    return tenant_id, owner_id


async def _queue_markdown_job(
    owner_sessionmaker,
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    tmp_path,
    markdown: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Queue a markdown import exactly like the API path does (source spooled,
    document+job persisted, committed)."""
    service = PersonalKnowledgeService(data_root=tmp_path)
    async with owner_sessionmaker() as session:
        result = await service.queue_markdown_import(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            title="Lifecycle regression doc",
            markdown=markdown,
            source_kind="paste",
            source_uri=None,
            created_by_user_id=owner_id,
            agent_searchable=True,
            sensitivity="internal",
        )
        await session.commit()
    return result.document_id, result.job_id


async def _read_job(owner_sessionmaker, job_id: uuid.UUID) -> tuple[str, int]:
    """Read (status, attempt_count) inside the session — a detached instance
    cannot be refreshed after rollback."""
    async with owner_sessionmaker() as session:
        job = (await session.execute(select(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job_id))).scalar_one()
        snapshot = (str(job.status or ""), int(job.attempt_count or 0))
        await session.rollback()
        return snapshot


@pytest.fixture
async def complete_schema(owner_engine):
    async with owner_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _await_worker(task: "asyncio.Task", *, timeout: float = 60):
    """Always land a gated worker: wait with a hang-detector timeout; on
    timeout cancel AND await the cancellation so no task/lock is left behind
    in the shared container."""
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


async def test_import_worker_commits_running_attempt_before_long_conversion(
    complete_schema, owner_sessionmaker, tmp_path
):
    """A concurrent session must observe committed running + attempt 1 while
    conversion is still in flight, and the terminal state after release."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    markdown = f"# Lifecycle\n\n{MARKER_EN} and {MARKER_ZH} body text."
    document_id, job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=markdown,
    )

    gate = asyncio.Event()
    entered = asyncio.Event()
    service = PersonalKnowledgeService(data_root=tmp_path)
    original_ingest = service.ingest_markdown

    async def gated_ingest(session, **kwargs):
        entered.set()
        await gate.wait()
        return await original_ingest(session, **kwargs)

    service.ingest_markdown = gated_ingest  # type: ignore[method-assign]

    worker_task = asyncio.create_task(
        service.process_import_jobs(
            None,
            session_factory=lambda: _session_context(owner_sessionmaker),
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            current_user_id=owner_id,
            limit=1,
            statuses=("queued",),
        )
    )
    try:
        # The claim is committed before the body starts: waiting for the body
        # to be entered proves running + attempt 1 is durably visible. The
        # timeout is a hang detector only.
        await asyncio.wait_for(entered.wait(), timeout=30)

        # Concurrent reader (fresh session, READ COMMITTED): the claim must be
        # durably visible — running + attempt 1 — before conversion completes.
        observed_status, observed_attempts = await _read_job(owner_sessionmaker, job_id)
        assert observed_status == "running", f"expected committed running, saw {observed_status}"
        assert observed_attempts == 1
    finally:
        gate.set()
        summary = await _await_worker(worker_task)

    assert summary.succeeded == 1, summary.results
    final_status, final_attempts = await _read_job(owner_sessionmaker, job_id)
    assert final_status in {"ready", "degraded"}
    # The worker claim owns attempt accounting exactly once: the mid-flight
    # claim commit and the terminal state both count exactly one attempt.
    assert final_attempts == 1


async def test_second_worker_skips_locked_job_and_claims_next(complete_schema, owner_sessionmaker, tmp_path):
    """SKIP LOCKED is the sole claim authority: the job whose claim row lock worker A holds is never processed by worker B."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path)
    job_ids = []
    async with owner_sessionmaker() as session:
        for index in range(2):
            result = await service.queue_markdown_import(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_id,
                title=f"Skip-locked doc {index}",
                markdown=f"# Doc {index}\n\n{MARKER_EN} number {index}",
                source_kind="paste",
                source_uri=None,
                created_by_user_id=owner_id,
                agent_searchable=True,
                sensitivity="internal",
            )
            job_ids.append(result.job_id)
        await session.commit()

    claim_entered = asyncio.Event()
    release_claim = asyncio.Event()
    held_job_ids: list[str] = []
    original_claim = PersonalKnowledgeService._claim_import_job_for_processing

    async def held_claim(self, session, *, job, metadata):
        await original_claim(self, session, job=job, metadata=metadata)
        if not held_job_ids:
            # Capture whichever job A actually claimed first (claim order is
            # not insertion order), then keep its claim transaction open.
            held_job_ids.append(str(job.id))
            claim_entered.set()
            await release_claim.wait()

    service_a = PersonalKnowledgeService(data_root=tmp_path)
    service_a._claim_import_job_for_processing = held_claim.__get__(  # type: ignore[method-assign]
        service_a, PersonalKnowledgeService
    )

    task_a = asyncio.create_task(
        service_a.process_import_jobs(
            None,
            session_factory=lambda: _session_context(owner_sessionmaker),
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            current_user_id=owner_id,
            limit=2,
            statuses=("queued",),
        )
    )
    # Deterministic synchronization: B starts only after A's claim transaction
    # holds the captured job's row lock. Timeouts are hang detectors only;
    # the held claim is always released AND the worker always awaited so no
    # task/lock is left behind on a failure path.
    try:
        await asyncio.wait_for(claim_entered.wait(), timeout=30)
        service_b = PersonalKnowledgeService(data_root=tmp_path)
        summary_b = await service_b.process_import_jobs(
            None,
            session_factory=lambda: _session_context(owner_sessionmaker),
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            current_user_id=owner_id,
            limit=2,
            statuses=("queued",),
        )
    finally:
        release_claim.set()
        summary_a = await _await_worker(task_a)

    # B claimed only the unlocked job; A finished its own held claim. The
    # captured job id was never processed by B, and each worker ran exactly
    # one job to terminal success.
    processed_by_b = {entry["job_id"] for entry in summary_b.results}
    assert held_job_ids[0] not in processed_by_b, processed_by_b
    assert summary_b.attempted == 1 and summary_b.succeeded == 1, summary_b.results
    assert summary_a.attempted == 1 and summary_a.succeeded == 1, summary_a.results


class _session_context:
    """Adapt an async_sessionmaker() call into the async-context shape the
    service expects from session_factory (mirrors tenant_scoped_session)."""

    def __init__(self, sessionmaker):
        self._sessionmaker = sessionmaker
        self._session = None

    async def __aenter__(self):
        self._session = self._sessionmaker()
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        if self._session is not None:
            await self._session.close()
        return False


async def _force_job_state(
    owner_sessionmaker,
    job_id: uuid.UUID,
    *,
    status: str,
    attempt_count: int,
    updated_at: datetime | None = None,
    metadata_patch: dict | None = None,
) -> None:
    """Move a job row into an exact lifecycle state (direct UPDATE, no service)."""
    async with owner_sessionmaker() as session:
        job = (await session.execute(select(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job_id))).scalar_one()
        job.status = status
        job.attempt_count = attempt_count
        if updated_at is not None:
            job.updated_at = updated_at
        if metadata_patch:
            job.job_metadata_json = {**dict(job.job_metadata_json or {}), **metadata_patch}
        await session.commit()


async def _read_job_full(owner_sessionmaker, job_id: uuid.UUID) -> tuple[str, int, dict]:
    async with owner_sessionmaker() as session:
        job = (await session.execute(select(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job_id))).scalar_one()
        snapshot = (str(job.status or ""), int(job.attempt_count or 0), dict(job.job_metadata_json or {}))
        await session.rollback()
        return snapshot


async def test_final_allowed_attempt_executes_and_reaches_terminal(complete_schema, owner_sessionmaker, tmp_path):
    """B1: a job with attempt_count == max_attempts - 1 must execute its final
    allowed claim. The claim statement admits attempt_count < max; the claim
    increments to exactly max; the run guard must not reject that final claim."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    markdown = f"# Final attempt\n\n{MARKER_EN} final attempt body."
    document_id, job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=markdown,
    )
    await _force_job_state(owner_sessionmaker, job_id, status="queued", attempt_count=4)

    service = PersonalKnowledgeService(data_root=tmp_path)
    summary = await service.process_import_jobs(
        None,
        session_factory=lambda: _session_context(owner_sessionmaker),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=1,
        statuses=("queued",),
        max_attempts=5,
    )

    assert summary.succeeded == 1, summary.results
    final_status, final_attempts, _metadata = await _read_job_full(owner_sessionmaker, job_id)
    assert final_status in {"ready", "degraded"}, final_status
    assert final_attempts == 5


async def test_failed_job_is_not_auto_reselected_by_default_worker(complete_schema, owner_sessionmaker, tmp_path):
    """B4: normal workers select queued jobs and stale-running leases only. A
    failed job under the attempt ceiling must stay failed until the explicit
    retry CAS requeues it — never silently re-run by the default worker."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path)
    failed_document_id, failed_job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Failed doc\n\n{MARKER_EN} failed job body.",
    )
    _queued_document_id, queued_job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Healthy doc\n\n{MARKER_EN} healthy job body.",
    )
    await _force_job_state(owner_sessionmaker, failed_job_id, status="failed", attempt_count=1)

    # Default worker invocation (no explicit statuses) — the API worker shape.
    summary = await service.process_import_jobs(
        None,
        session_factory=lambda: _session_context(owner_sessionmaker),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=5,
    )
    # Fleet stale-drain invocation.
    drain = await service.claim_and_process_stuck_jobs(
        None,
        session_factory=lambda: _session_context(owner_sessionmaker),
        limit=5,
        queued_grace_seconds=0,
    )

    failed_status, failed_attempts, _ = await _read_job_full(owner_sessionmaker, failed_job_id)
    assert failed_status == "failed", failed_status
    assert failed_attempts == 1, failed_attempts
    queued_status, _queued_attempts, _ = await _read_job_full(owner_sessionmaker, queued_job_id)
    assert queued_status in {"ready", "degraded"}, (summary.results, drain.results, queued_status)


async def test_stale_claim_loser_cannot_overwrite_reclaimed_job(complete_schema, owner_sessionmaker, tmp_path):
    """B2 two-worker race: worker A claims and is gated DURING its phase-2
    work (no row lock is held across conversion); worker B reclaims the stale
    lease, executes, and commits. Worker A's final opaque-token CAS must fail,
    roll back every staged document/segment/job write, and report a typed
    claim_lost outcome."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    document_id, job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Fencing\n\n{MARKER_EN} fencing body {MARKER_ZH}.",
    )

    entered_work = asyncio.Event()
    release_work = asyncio.Event()
    service_a = PersonalKnowledgeService(data_root=tmp_path)
    original_ingest = service_a.ingest_markdown

    async def gated_ingest(session, **kwargs):
        # A's claim (running + attempt 1) is already committed at this point;
        # the phase-2 transaction holds NO lock on the job row yet.
        entered_work.set()
        await release_work.wait()
        return await original_ingest(session, **kwargs)

    service_a.ingest_markdown = gated_ingest  # type: ignore[method-assign]
    worker_a = asyncio.create_task(
        service_a.process_import_jobs(
            None,
            session_factory=lambda: _session_context(owner_sessionmaker),
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            current_user_id=owner_id,
            limit=1,
            statuses=("queued",),
        )
    )
    # Timeouts are hang detectors only; the gate is always released AND worker
    # A is always awaited so no task/lock is left behind on a failure path.
    try:
        await asyncio.wait_for(entered_work.wait(), timeout=30)

        # Worker B reclaims the stale running lease and finishes the whole job.
        service_b = PersonalKnowledgeService(data_root=tmp_path)
        summary_b = await service_b.claim_and_process_stuck_jobs(
            None,
            session_factory=lambda: _session_context(owner_sessionmaker),
            limit=1,
            queued_grace_seconds=30,
            running_timeout_seconds=0,
        )
    finally:
        release_work.set()
        summary_a = await _await_worker(worker_a)
    assert summary_b.succeeded == 1, summary_b.results
    status_b, attempts_b, metadata_b = await _read_job_full(owner_sessionmaker, job_id)
    assert status_b in {"ready", "degraded"}, status_b
    assert attempts_b == 2, attempts_b
    token_b = str(metadata_b.get("claimed_token") or "")

    async with owner_sessionmaker() as session:
        segments_b = (
            (
                await session.execute(
                    select(KnowledgeSegment.id)
                    .where(KnowledgeSegment.document_id == document_id)
                    .order_by(KnowledgeSegment.position.asc())
                )
            )
            .scalars()
            .all()
        )
        await session.rollback()
    assert segments_b

    # Worker A already finished (awaited in the finally above); its finalize
    # must have lost the fence.
    outcomes_a = [entry["status"] for entry in summary_a.results]
    assert outcomes_a == ["claim_lost"], summary_a.results
    assert summary_a.succeeded == 0, summary_a.results

    # Every staged write rolled back: job, document, and segments stay B's.
    status_after, attempts_after, metadata_after = await _read_job_full(owner_sessionmaker, job_id)
    assert status_after == status_b
    assert attempts_after == attempts_b
    assert str(metadata_after.get("claimed_token") or "") == token_b
    async with owner_sessionmaker() as session:
        segments_after = (
            (
                await session.execute(
                    select(KnowledgeSegment.id)
                    .where(KnowledgeSegment.document_id == document_id)
                    .order_by(KnowledgeSegment.position.asc())
                )
            )
            .scalars()
            .all()
        )
        document_after = (
            await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
        ).scalar_one()
        doc_status_after = str(document_after.status or "")
        await session.rollback()
    assert [str(segment_id) for segment_id in segments_after] == [str(segment_id) for segment_id in segments_b]
    assert doc_status_after in {"ready", "degraded"}


async def test_phase_two_with_foreign_token_aborts_before_work(complete_schema, owner_sessionmaker, tmp_path):
    """B2 fast path: when the committed claim token already belongs to another
    worker, phase 2 aborts as typed claim_lost before any ingest runs — no
    staged writes at all."""
    from app.services.personal_knowledge_service import PersonalKnowledgeClaimLost

    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    _document_id, job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Token\n\n{MARKER_EN} token body.",
    )
    await _force_job_state(
        owner_sessionmaker,
        job_id,
        status="running",
        attempt_count=2,
        metadata_patch={"claimed_token": "token-b"},
    )

    service_a = PersonalKnowledgeService(data_root=tmp_path)
    ingest_calls: list[str] = []

    async def counting_ingest(session, **kwargs):  # pragma: no cover - must not run
        ingest_calls.append("ingest")
        raise AssertionError("stale phase-2 claim must not run ingest")

    service_a.ingest_markdown = counting_ingest  # type: ignore[method-assign]
    async with owner_sessionmaker() as session:
        with pytest.raises(PersonalKnowledgeClaimLost):
            await service_a._process_claimed_job_phase_two(
                session,
                job_id=job_id,
                claimed_token="token-a",
                claimed_attempt=1,
                tenant_id=tenant_id,
                owner_user_id=owner_id,
                max_attempts=5,
            )
        await session.rollback()
    assert ingest_calls == []

    status_after, attempts_after, metadata_after = await _read_job_full(owner_sessionmaker, job_id)
    assert status_after == "running"
    assert attempts_after == 2
    assert str(metadata_after.get("claimed_token") or "") == "token-b"


async def test_deleted_document_leaves_no_running_job(complete_schema, owner_sessionmaker, tmp_path):
    """B3: a document deleted before its job is claimed cascade-removes the
    job; the worker finishes the rest of the batch and leaves no running row."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path)
    deleted_document_id, _deleted_job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Deleted\n\n{MARKER_EN} deleted doc body.",
    )
    _kept_document_id, kept_job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Kept\n\n{MARKER_EN} kept doc body.",
    )
    async with owner_sessionmaker() as session:
        document = (
            await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == deleted_document_id))
        ).scalar_one()
        await session.delete(document)
        await session.commit()

    summary = await service.process_import_jobs(
        None,
        session_factory=lambda: _session_context(owner_sessionmaker),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=5,
        statuses=("queued",),
    )
    assert summary.succeeded == 1, summary.results
    kept_status, _, _ = await _read_job_full(owner_sessionmaker, kept_job_id)
    assert kept_status in {"ready", "degraded"}
    async with owner_sessionmaker() as session:
        running_rows = (
            await session.execute(
                select(KnowledgeIndexJob).where(
                    KnowledgeIndexJob.tenant_id == tenant_id,
                    KnowledgeIndexJob.status == "running",
                )
            )
        ).all()
        await session.rollback()
    assert running_rows == []


async def test_ingest_returning_none_terminalizes_document_missing(complete_schema, owner_sessionmaker, tmp_path):
    """B3: when the ingest body returns None (document no longer readable for
    this scope), the claimed job must reach a typed terminal failure instead
    of staying running until the stale-claim timeout."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path)
    # A rebuild-style job carries no queued_import_kind: the worker body is
    # rebuild_personal_document_index, whose None return is the production
    # "document missing / no longer readable" signal.
    async with owner_sessionmaker() as session:
        result = await service.ingest_markdown(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            title="None-exit doc",
            markdown=f"# None exit\n\n{MARKER_EN} none exit body.",
            source_kind="paste",
            source_uri=None,
            created_by_user_id=owner_id,
            agent_searchable=True,
            sensitivity="internal",
        )
        await session.commit()
    job_id = result.job_id
    await _force_job_state(owner_sessionmaker, job_id, status="queued", attempt_count=0)

    async def none_rebuild(*args, **kwargs):
        return None

    service.rebuild_personal_document_index = none_rebuild  # type: ignore[method-assign]
    summary = await service.process_import_jobs(
        None,
        session_factory=lambda: _session_context(owner_sessionmaker),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=1,
        statuses=("queued",),
    )

    final_status, _attempts, metadata = await _read_job_full(owner_sessionmaker, job_id)
    assert final_status != "running", (summary.results, final_status)
    assert final_status == "failed", (summary.results, final_status)
    assert metadata.get("error") == "document_missing", metadata


async def test_final_claim_crash_stale_drain_terminalizes_without_rerun(complete_schema, owner_sessionmaker, tmp_path):
    """B1 recovery: a worker that crashed after its final allowed claim leaves
    status=running at attempt_count==max. The stale drain must select that row
    (no blanket attempt filter), terminalize it as attempt-limit-exhausted
    WITHOUT rerunning the import body, and leave a typed terminal failure."""
    from datetime import datetime, timedelta, timezone

    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    document_id, job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Crash\n\n{MARKER_EN} crash body.",
    )
    await _force_job_state(
        owner_sessionmaker,
        job_id,
        status="running",
        attempt_count=5,
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=3600),
        metadata_patch={"claimed_token": "crashed-worker-token"},
    )

    service = PersonalKnowledgeService(data_root=tmp_path)
    ingest_calls: list[str] = []

    async def counting_ingest(session, **kwargs):  # pragma: no cover - must not run
        ingest_calls.append("ingest")
        raise AssertionError("attempt-exhausted stale claim must not rerun work")

    service.ingest_markdown = counting_ingest  # type: ignore[method-assign]
    summary = await service.claim_and_process_stuck_jobs(
        None,
        session_factory=lambda: _session_context(owner_sessionmaker),
        limit=5,
        queued_grace_seconds=30,
        running_timeout_seconds=600,
        max_attempts=5,
    )

    assert ingest_calls == []
    # The terminalized stale row counts as processed work: attempted reflects
    # it and limit stays bounded for a fleet of exhausted rows.
    assert summary.attempted == 1, summary.results
    assert summary.failed == 1, summary.results
    final_status, final_attempts, metadata = await _read_job_full(owner_sessionmaker, job_id)
    assert final_status == "failed", final_status
    assert final_attempts == 5, final_attempts
    assert metadata.get("error") == "personal_kb_import_attempt_limit_exceeded", metadata
    # The never-consumable document must not stay permanently queued in the
    # /knowledge read model: it terminalizes to failed in the same fenced
    # transaction (a rebuild of a ready/degraded document would keep its
    # prior consumable status instead).
    async with owner_sessionmaker() as session:
        document = (
            await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
        ).scalar_one()
        document_status = str(document.status or "")
        await session.rollback()
    assert document_status == "failed", document_status
    # The exhausted job is terminal: a second drain finds nothing to do.
    second = await PersonalKnowledgeService(data_root=tmp_path).claim_and_process_stuck_jobs(
        None,
        session_factory=lambda: _session_context(owner_sessionmaker),
        limit=5,
        queued_grace_seconds=30,
        running_timeout_seconds=600,
        max_attempts=5,
    )
    assert second.attempted == 0, second.results


async def test_retry_clears_terminal_state_from_requeued_job(complete_schema, owner_sessionmaker, tmp_path):
    """Codex-1: requeueing a failed/cancelled job must clear its current-state
    failure fields — the fresh queued job must not expose a stale error or
    cancelled_at through the read model."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path)
    _document_id, job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Retry hygiene\n\n{MARKER_EN} retry hygiene body.",
    )
    await _force_job_state(
        owner_sessionmaker,
        job_id,
        status="failed",
        attempt_count=1,
        metadata_patch={
            "error": "conversion_failed",
            "warnings": ["conversion_failed:RuntimeError"],
            "failure_exception": "RuntimeError",
            "failed_at": "2026-08-25T00:00:00+00:00",
            "finished_at": "2026-08-25T00:00:01+00:00",
        },
    )

    async with owner_sessionmaker() as session:
        summary = await service.retry_import_job(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            job_id=job_id,
            current_user_id=owner_id,
        )
        await session.commit()
    assert summary is not None
    assert summary.lifecycle_status == "queued"
    assert summary.error_code is None
    assert summary.cancelled_at is None

    status, _attempts, metadata = await _read_job_full(owner_sessionmaker, job_id)
    assert status == "queued"
    for stale_key in ("error", "warnings", "failure_exception", "failed_at", "finished_at", "cancelled_at"):
        assert stale_key not in metadata, (stale_key, metadata)
    assert metadata.get("retried_at")

    # A cancelled job requeued by retry also drops its cancelled marker.
    await _force_job_state(
        owner_sessionmaker,
        job_id,
        status="cancelled",
        attempt_count=1,
        metadata_patch={"cancelled_at": "2026-08-25T01:00:00+00:00"},
    )
    async with owner_sessionmaker() as session:
        summary2 = await service.retry_import_job(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            job_id=job_id,
            current_user_id=owner_id,
        )
        await session.commit()
    assert summary2 is not None
    assert summary2.cancelled_at is None
    _status2, _attempts2, metadata2 = await _read_job_full(owner_sessionmaker, job_id)
    assert "cancelled_at" not in metadata2


async def test_worker_error_fail_write_terminalizes_transient_document(complete_schema, owner_sessionmaker, tmp_path):
    """Codex-2a: an unexpected phase-2 fault lands on the fenced fail-write, which terminalizes the transient document in the same transaction."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    document_id, job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Fail write\n\n{MARKER_EN} fail write body.",
    )

    service = PersonalKnowledgeService(data_root=tmp_path)

    async def exploding_phase_two(session, **kwargs):
        raise RuntimeError("simulated transient infrastructure fault")

    service._process_claimed_job_phase_two = exploding_phase_two  # type: ignore[method-assign]
    summary = await service.process_import_jobs(
        None,
        session_factory=lambda: _session_context(owner_sessionmaker),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=1,
        statuses=("queued",),
    )

    assert summary.failed == 1, summary.results
    final_status, _attempts, metadata = await _read_job_full(owner_sessionmaker, job_id)
    assert final_status == "failed", final_status
    assert metadata.get("error") == "worker_error", metadata
    assert metadata.get("failure_exception") == "RuntimeError"
    async with owner_sessionmaker() as session:
        document = (
            await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
        ).scalar_one()
        document_status = str(document.status or "")
        await session.rollback()
    assert document_status == "failed", document_status


async def test_fail_write_with_lost_lease_leaves_job_and_document_untouched(
    complete_schema, owner_sessionmaker, tmp_path
):
    """Codex-2b: a fenced fail-write whose lease is stale (CAS miss) must not
    mutate the job row or the document at all."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path)
    document_id, job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Lost lease\n\n{MARKER_EN} lost lease body.",
    )
    await service.process_import_jobs(
        None,
        session_factory=lambda: _session_context(owner_sessionmaker),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=1,
        statuses=("queued",),
    )
    ready_status, ready_attempts, _ = await _read_job_full(owner_sessionmaker, job_id)
    assert ready_status in {"ready", "degraded"}

    async with owner_sessionmaker() as session:
        wrote = await service._fail_claimed_job_after_worker_error(
            session,
            job_id=job_id,
            claimed_token="stale-token",
            claimed_attempt=0,
            code="worker_error",
            exception_name="RuntimeError",
        )
        await session.commit()
    assert wrote is False

    status_after, attempts_after, metadata_after = await _read_job_full(owner_sessionmaker, job_id)
    assert status_after == ready_status
    assert attempts_after == ready_attempts
    assert "worker_error" not in str(metadata_after.get("error") or "")
    async with owner_sessionmaker() as session:
        document = (
            await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
        ).scalar_one()
        document_status = str(document.status or "")
        await session.rollback()
    assert document_status in {"ready", "degraded"}, document_status


async def test_cancel_terminalizes_transient_document_in_same_transaction(
    complete_schema, owner_sessionmaker, tmp_path
):
    """Cancel of a queued initial import ends the transient document as cancelled, not permanently queued."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path)
    document_id, job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Cancel\n\n{MARKER_EN} cancel body.",
    )

    async with owner_sessionmaker() as session:
        summary = await service.cancel_import_job(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            job_id=job_id,
            current_user_id=owner_id,
        )
        await session.commit()
    assert summary is not None
    assert summary.lifecycle_status == "cancelled"
    assert summary.cancelled_at

    async with owner_sessionmaker() as session:
        document = (
            await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
        ).scalar_one()
        document_status = str(document.status or "")
        document_error = str(dict(document.doc_metadata_json or {}).get("error") or "")
        await session.rollback()
    assert document_status == "failed", document_status
    assert document_error == "cancelled", document_error


async def test_cancel_preserves_consumable_rebuild_document(complete_schema, owner_sessionmaker, tmp_path):
    """Cancel of a queued rebuild job leaves the ready document consumable and untouched."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path)
    async with owner_sessionmaker() as session:
        result = await service.ingest_markdown(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            title="Ready rebuild doc",
            markdown=f"# Ready\n\n{MARKER_EN} ready body.",
            source_kind="paste",
            source_uri=None,
            created_by_user_id=owner_id,
            agent_searchable=True,
            sensitivity="internal",
        )
        await session.commit()
    job_id = result.job_id
    document_id = result.document_id
    assert job_id is not None
    await _force_job_state(owner_sessionmaker, job_id, status="queued", attempt_count=0)

    async with owner_sessionmaker() as session:
        summary = await service.cancel_import_job(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            job_id=job_id,
            current_user_id=owner_id,
        )
        await session.commit()
    assert summary is not None and summary.lifecycle_status == "cancelled"

    async with owner_sessionmaker() as session:
        document = (
            await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
        ).scalar_one()
        document_status = str(document.status or "")
        await session.rollback()
    assert document_status in {"ready", "degraded"}, document_status


async def test_archive_during_running_import_survives_worker_completion(complete_schema, owner_sessionmaker, tmp_path):
    """Archive committed mid-import owns document.status; the worker still completes content but never flips it back."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    document_id, job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Archive race\n\n{MARKER_EN} archive race body.",
    )

    entered_work = asyncio.Event()
    release_work = asyncio.Event()
    service = PersonalKnowledgeService(data_root=tmp_path)
    original_ingest = service.ingest_markdown

    async def gated_ingest(session, **kwargs):
        entered_work.set()
        await release_work.wait()
        return await original_ingest(session, **kwargs)

    service.ingest_markdown = gated_ingest  # type: ignore[method-assign]
    worker = asyncio.create_task(
        service.process_import_jobs(
            None,
            session_factory=lambda: _session_context(owner_sessionmaker),
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            current_user_id=owner_id,
            limit=1,
            statuses=("queued",),
        )
    )
    try:
        await asyncio.wait_for(entered_work.wait(), timeout=30)

        # The user archives while the worker is inside phase 2.
        async with owner_sessionmaker() as session:
            archived = await service.patch_personal_document(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_id,
                document_id=document_id,
                current_user_id=owner_id,
                agent_id=None,
                status="archived",
            )
            await session.commit()
        assert archived is not None and archived.status == "archived"
    finally:
        release_work.set()
        summary = await _await_worker(worker)
    assert summary.succeeded == 1, summary.results

    # The document stays archived; the worker's real final consumable status
    # is recorded as the restore target.
    async with owner_sessionmaker() as session:
        document = (
            await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
        ).scalar_one()
        final_doc_status = str(document.status or "")
        archived_from = str(dict(document.doc_metadata_json or {}).get("archived_from_status") or "")
        await session.rollback()
    assert final_doc_status == "archived", final_doc_status
    assert archived_from in {"ready", "degraded"}, archived_from

    # Agent search/read cannot consume it; the owner workbench still can.
    from app.services.personal_knowledge_access import AgentRuntimePrincipal

    from app.models.agent import Agent as AgentModel

    agent_id = uuid.uuid4()
    async with owner_sessionmaker() as session:
        session.add(AgentModel(id=agent_id, name="Archive Race Agent", creator_id=owner_id, tenant_id=tenant_id))
        await session.commit()
    agent_principal = AgentRuntimePrincipal(
        agent_id=agent_id,
        requester_user_id=owner_id,
        session_id=str(uuid.uuid4()),
        purpose="interactive_session",
    )
    async with owner_sessionmaker() as session:
        agent_search = await service.search_personal_with_authority(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            query=MARKER_EN,
            principal=agent_principal,
            limit=5,
        )
        agent_read = await service.get_personal_document_with_authority(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            document_id=document_id,
            principal=agent_principal,
        )
        await session.rollback()
    assert agent_search.hits == []
    assert agent_read.document is None

    # Restore recovers the worker's final consumable status; the agent can
    # search and read again.
    async with owner_sessionmaker() as session:
        restored = await service.restore_personal_document(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            document_id=document_id,
            current_user_id=owner_id,
        )
        await session.commit()
    assert restored is not None and restored.status == archived_from
    async with owner_sessionmaker() as session:
        agent_search_after = await service.search_personal_with_authority(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            query=MARKER_EN,
            principal=agent_principal,
            limit=5,
        )
        agent_read_after = await service.get_personal_document_with_authority(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            document_id=document_id,
            principal=agent_principal,
        )
        await session.rollback()
    assert agent_search_after.hits
    assert agent_read_after.status == "ok"
    assert agent_read_after.document is not None
    assert agent_read_after.document.source_ref == f"kb://person/{owner_id}/documents/{document_id}"


async def test_archive_during_running_rebuild_survives_worker_completion(complete_schema, owner_sessionmaker, tmp_path):
    """The rebuild path honors the same archive ownership boundary."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path)
    async with owner_sessionmaker() as session:
        ingest = await service.ingest_markdown(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            title="Rebuild archive race",
            markdown=f"# Rebuild race\n\n{MARKER_EN} rebuild race body.",
            source_kind="paste",
            source_uri=None,
            created_by_user_id=owner_id,
            agent_searchable=True,
            sensitivity="internal",
        )
        await session.commit()
    document_id = ingest.document_id
    job_id = ingest.job_id
    assert job_id is not None
    await _force_job_state(owner_sessionmaker, job_id, status="queued", attempt_count=0)

    entered_work = asyncio.Event()
    release_work = asyncio.Event()
    original_ingest = service.ingest_markdown

    async def gated_ingest(session, **kwargs):
        entered_work.set()
        await release_work.wait()
        return await original_ingest(session, **kwargs)

    service.ingest_markdown = gated_ingest  # type: ignore[method-assign]
    worker = asyncio.create_task(
        service.process_import_jobs(
            None,
            session_factory=lambda: _session_context(owner_sessionmaker),
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            current_user_id=owner_id,
            limit=1,
            statuses=("queued",),
        )
    )
    try:
        await asyncio.wait_for(entered_work.wait(), timeout=30)

        async with owner_sessionmaker() as session:
            archived = await service.patch_personal_document(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_id,
                document_id=document_id,
                current_user_id=owner_id,
                agent_id=None,
                status="archived",
            )
            await session.commit()
        assert archived is not None and archived.status == "archived"
    finally:
        release_work.set()
        summary = await _await_worker(worker)
    assert summary.succeeded == 1, summary.results

    async with owner_sessionmaker() as session:
        document = (
            await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
        ).scalar_one()
        final_doc_status = str(document.status or "")
        archived_from = str(dict(document.doc_metadata_json or {}).get("archived_from_status") or "")
        await session.rollback()
    assert final_doc_status == "archived", final_doc_status
    assert archived_from in {"ready", "degraded"}, archived_from


async def test_rebuild_of_archived_document_records_true_final_restore_target(
    complete_schema, owner_sessionmaker, tmp_path
):
    """Platform lifecycle fields win over caller/old metadata in the final merge."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path)
    async with owner_sessionmaker() as session:
        ingest = await service.ingest_markdown(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            title="Merge order doc",
            markdown=f"# Merge order\n\n{MARKER_EN} merge order body.",
            source_kind="paste",
            source_uri=None,
            created_by_user_id=owner_id,
            agent_searchable=True,
            sensitivity="internal",
        )
        await session.commit()
    document_id = ingest.document_id

    async with owner_sessionmaker() as session:
        await service.patch_personal_document(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            document_id=document_id,
            current_user_id=owner_id,
            agent_id=None,
            status="archived",
        )
        # Simulate a stale restore target from older history; the real final
        # status of this rebuild (degraded in the test environment, where the
        # graph extractor is unavailable) must replace it, never the reverse.
        document = (
            await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
        ).scalar_one()
        document.doc_metadata_json = {
            **dict(document.doc_metadata_json or {}),
            "archived_from_status": "ready",
        }
        await session.commit()

    async with owner_sessionmaker() as session:
        result = await service.rebuild_personal_document_index(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            document_id=document_id,
            current_user_id=owner_id,
        )
        await session.commit()
    assert result is not None and result.status in {"ready", "degraded"}

    async with owner_sessionmaker() as session:
        document = (
            await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
        ).scalar_one()
        final_status = str(document.status or "")
        archived_from = str(dict(document.doc_metadata_json or {}).get("archived_from_status") or "")
        await session.rollback()
    assert final_status == "archived", final_status
    assert archived_from == result.status == "degraded", (archived_from, result.status)


class _HoldAfterFirstExecute:
    """Session proxy: lets the archive take the document row lock, then holds
    before the mutation/flush so a worker can compete for the same row."""

    def __init__(self, real_session, entered: asyncio.Event, release: asyncio.Event):
        self._real = real_session
        self._entered = entered
        self._release = release
        self._held = False

    async def execute(self, *args, **kwargs):
        result = await self._real.execute(*args, **kwargs)
        if not self._held:
            self._held = True
            self._entered.set()
            await self._release.wait()
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


async def test_archive_holding_lock_first_worker_waits_and_respects_it(complete_schema, owner_sessionmaker, tmp_path):
    """Reverse order: archive holds the document lock first; the worker waits, then keeps archived and records the true restore target."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    document_id, _job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=f"# Reverse archive\n\n{MARKER_EN} reverse archive body.",
    )
    service = PersonalKnowledgeService(data_root=tmp_path)

    entered = asyncio.Event()
    release = asyncio.Event()
    worker_entered = asyncio.Event()
    archive_result: list = []

    async def run_archive():
        async with owner_sessionmaker() as real_session:
            gated = _HoldAfterFirstExecute(real_session, entered, release)
            archived = await service.patch_personal_document(
                gated,
                tenant_id=tenant_id,
                owner_user_id=owner_id,
                document_id=document_id,
                current_user_id=owner_id,
                agent_id=None,
                status="archived",
            )
            archive_result.append(archived)
            await real_session.commit()

    archive_task = asyncio.create_task(run_archive())
    worker: asyncio.Task | None = None
    try:
        # Archive now holds the document row lock (paused before mutation/flush).
        await asyncio.wait_for(entered.wait(), timeout=30)

        # The worker starts and competes for the same document row; it signals
        # right before its ingest body would run.
        original_ingest = service.ingest_markdown

        async def signaling_ingest(session, **kwargs):
            worker_entered.set()
            return await original_ingest(session, **kwargs)

        service.ingest_markdown = signaling_ingest  # type: ignore[method-assign]
        worker = asyncio.create_task(
            service.process_import_jobs(
                None,
                session_factory=lambda: _session_context(owner_sessionmaker),
                tenant_id=tenant_id,
                owner_user_id=owner_id,
                current_user_id=owner_id,
                limit=1,
                statuses=("queued",),
            )
        )
        await asyncio.wait_for(worker_entered.wait(), timeout=30)
    finally:
        # Every gated party is always released AND awaited; each cleanup is
        # independent so a failure in one never skips the other.
        release.set()
        try:
            if worker is not None:
                summary = await _await_worker(worker)
        finally:
            await _await_worker(archive_task)
    assert summary.succeeded == 1, summary.results
    assert archive_result and archive_result[0].status == "archived"

    async with owner_sessionmaker() as session:
        document = (
            await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
        ).scalar_one()
        final_status = str(document.status or "")
        metadata = dict(document.doc_metadata_json or {})
        await session.rollback()
    assert final_status == "archived", final_status
    # The restore target reflects the worker's real final consumable state,
    # never a stale pre-worker snapshot; fresh worker metadata survived.
    assert metadata.get("archived_from_status") in {"ready", "degraded"}, metadata
    assert metadata.get("archived_at")


async def test_source_missing_recovery_via_reupload_and_explicit_retry(complete_schema, owner_sessionmaker, tmp_path):
    """source_missing is recoverable: re-uploading the same bytes restores the evidence; an explicit retry then completes."""
    tenant_id, owner_id = await _seed_owner(owner_sessionmaker)
    service = PersonalKnowledgeService(data_root=tmp_path)
    markdown = f"# Recovery\n\n{MARKER_EN} recovery body."
    document_id, job_id = await _queue_markdown_job(
        owner_sessionmaker,
        tenant_id=tenant_id,
        owner_id=owner_id,
        tmp_path=tmp_path,
        markdown=markdown,
    )

    # Destroy the spooled evidence, then let the worker land a typed failure.
    for spool in tmp_path.rglob("*.md"):
        spool.unlink()
    first = await service.process_import_jobs(
        None,
        session_factory=lambda: _session_context(owner_sessionmaker),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=1,
        statuses=("queued",),
    )
    assert first.failed == 1, first.results
    failed_status, _attempts, failed_metadata = await _read_job_full(owner_sessionmaker, job_id)
    assert failed_status == "failed"
    assert failed_metadata.get("error") == "source_missing"

    # Re-importing the same bytes rewrites the spool/artifact and dedupes to
    # the same document/job; the read model now offers a truthful retry.
    async with owner_sessionmaker() as session:
        requeued = await service.queue_markdown_import(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            title="Recovery",
            markdown=markdown,
            source_kind="paste",
            source_uri=None,
            created_by_user_id=owner_id,
            agent_searchable=True,
            sensitivity="internal",
        )
        await session.commit()
    assert requeued.document_id == document_id
    assert requeued.job_id == job_id
    async with owner_sessionmaker() as session:
        jobs = await service.list_import_jobs(session, tenant_id=tenant_id, owner_user_id=owner_id)
        await session.rollback()
    view = next(job for job in jobs if job.job_id == job_id)
    assert view.retryable is True, view

    # Explicit retry requeues; the worker completes with consumable segments
    # and citations.
    async with owner_sessionmaker() as session:
        retried = await service.retry_import_job(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            job_id=job_id,
            current_user_id=owner_id,
        )
        await session.commit()
    assert retried is not None and retried.lifecycle_status == "queued"
    second = await service.process_import_jobs(
        None,
        session_factory=lambda: _session_context(owner_sessionmaker),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=1,
        statuses=("queued",),
    )
    assert second.succeeded == 1, second.results
    final_status, _final_attempts, _final_metadata = await _read_job_full(owner_sessionmaker, job_id)
    assert final_status in {"ready", "degraded"}

    from app.services.personal_knowledge_access import HumanBrowserPrincipal

    async with owner_sessionmaker() as session:
        hits = await service.search_personal(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            query=MARKER_EN,
            principal=HumanBrowserPrincipal(user_id=owner_id),
            limit=5,
        )
        await session.rollback()
    assert hits
    assert all(hit.document_id and hit.segment_id for hit in hits)
