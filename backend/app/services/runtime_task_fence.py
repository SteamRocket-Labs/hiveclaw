"""Claim-fencing context for durable RuntimeTask workers.

Every worker claim receives a monotonically increasing version.  The version is
captured in the task context and checked again whenever that worker reloads or
updates the RuntimeTask, so a lease-expired worker cannot commit after a newer
worker has reclaimed the same run.
"""

from __future__ import annotations

import asyncio
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, TypeVar

from sqlalchemy import update

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, slots=True)
class RuntimeTaskFence:
    task_id: uuid.UUID
    claim_version: int
    worker_id: str
    claim_finished: asyncio.Event = field(default_factory=asyncio.Event, compare=False, repr=False)


class StaleRuntimeTaskFenceError(RuntimeError):
    """Raised when an expired worker attempts to mutate a reclaimed run."""


_CURRENT_RUNTIME_TASK_FENCE: ContextVar[RuntimeTaskFence | None] = ContextVar(
    "hive_runtime_task_fence",
    default=None,
)


def current_runtime_task_fence() -> RuntimeTaskFence | None:
    return _CURRENT_RUNTIME_TASK_FENCE.get()


def set_runtime_task_fence(
    *,
    task_id: uuid.UUID | str,
    claim_version: int,
    worker_id: str,
) -> Token[RuntimeTaskFence | None]:
    return _CURRENT_RUNTIME_TASK_FENCE.set(
        RuntimeTaskFence(
            task_id=task_id if isinstance(task_id, uuid.UUID) else uuid.UUID(str(task_id)),
            claim_version=max(0, int(claim_version)),
            worker_id=str(worker_id or "unknown"),
        )
    )


def reset_runtime_task_fence(token: Token[RuntimeTaskFence | None]) -> None:
    _CURRENT_RUNTIME_TASK_FENCE.reset(token)


def finish_current_runtime_task_claim(*, task_id: uuid.UUID | str | None = None) -> bool:
    """Stop lease renewal after this claim's durable state transition committed.

    This does not relax the fence: RuntimeTask reads and writes remain forbidden
    once the row is no longer ``running``.  It only lets non-task cleanup (for
    example releasing a workflow advisory lock) finish without a renewer racing
    the already-committed transition.
    """

    fence = current_runtime_task_fence()
    if fence is None:
        return False
    if task_id is not None and uuid.UUID(str(task_id)) != fence.task_id:
        return False
    fence.claim_finished.set()
    return True


def assert_runtime_task_fence(task: Any) -> None:
    fence = current_runtime_task_fence()
    if fence is None:
        return
    task_id = getattr(task, "id", None)
    if task_id is None or uuid.UUID(str(task_id)) != fence.task_id:
        return
    current_version = int(getattr(task, "claim_version", 0) or 0)
    if current_version != fence.claim_version:
        raise StaleRuntimeTaskFenceError(
            f"stale RuntimeTask worker fence for {fence.task_id}: "
            f"expected claim_version={fence.claim_version}, current={current_version}"
        )
    current_worker = str(getattr(task, "claimed_by", None) or "")
    current_status = str(getattr(task, "status", None) or "")
    claim_expires_at = getattr(task, "claim_expires_at", None)
    if isinstance(claim_expires_at, datetime) and claim_expires_at.tzinfo is None:
        claim_expires_at = claim_expires_at.replace(tzinfo=timezone.utc)
    claim_active = isinstance(claim_expires_at, datetime) and claim_expires_at > datetime.now(timezone.utc)
    if current_worker != fence.worker_id or current_status != "running" or not claim_active:
        raise StaleRuntimeTaskFenceError(
            f"stale RuntimeTask worker fence for {fence.task_id}: "
            f"expected claimed_by={fence.worker_id}, status=running, active lease; "
            f"current claimed_by={current_worker or None}, status={current_status or None}, "
            f"claim_expires_at={claim_expires_at.isoformat() if isinstance(claim_expires_at, datetime) else None}"
        )


async def renew_current_runtime_task_lease(*, lease_seconds: float) -> datetime | None:
    """Atomically renew the active worker lease or reject a stale worker.

    The update predicate binds task id, claim version, worker id, and running
    status. A reclaimed or terminal task therefore cannot be revived by an old
    worker immediately before a side effect.
    """
    fence = current_runtime_task_fence()
    if fence is None:
        return None

    from app.database import async_session, tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.services.tenant_resolver import resolve_tenant_for_runtime_task

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=max(0.03, float(lease_seconds)))
    tenant_id = await resolve_tenant_for_runtime_task(
        fence.task_id,
        session_factory=async_session,
    )
    if tenant_id is None:
        raise StaleRuntimeTaskFenceError(f"RuntimeTask {fence.task_id} no longer has an owning tenant")
    async with tenant_scoped_session(
        tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="runtime_task_fenced_lease_renewal",
    ) as db:
        result = await db.execute(
            update(RuntimeTask)
            .where(
                RuntimeTask.id == fence.task_id,
                RuntimeTask.claim_version == fence.claim_version,
                RuntimeTask.claimed_by == fence.worker_id,
                RuntimeTask.status == "running",
                RuntimeTask.claim_expires_at > now,
            )
            .values(claim_expires_at=expires_at)
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise StaleRuntimeTaskFenceError(
                f"stale RuntimeTask worker {fence.worker_id} cannot renew {fence.task_id} "
                f"at claim_version={fence.claim_version}"
            )
    return expires_at


async def run_claimed_runtime_task(
    work: Awaitable[_ResultT],
    *,
    task_id: uuid.UUID | str,
    claim_version: int,
    worker_id: str,
    lease_seconds: float,
) -> _ResultT:
    """Run claimed work with a ContextVar fence and fail-closed lease renewer."""
    token = set_runtime_task_fence(
        task_id=task_id,
        claim_version=claim_version,
        worker_id=worker_id,
    )
    work_task: asyncio.Task[_ResultT] | None = None
    renewer: asyncio.Task[None] | None = None
    fence = current_runtime_task_fence()
    if fence is None:  # pragma: no cover - set above; defensive against future refactors.
        raise RuntimeError("RuntimeTask fence was not installed")

    async def renew_loop() -> None:
        interval = max(0.01, float(lease_seconds) / 3.0)
        while not fence.claim_finished.is_set():
            try:
                await asyncio.wait_for(fence.claim_finished.wait(), timeout=interval)
            except TimeoutError:
                await renew_current_runtime_task_lease(lease_seconds=lease_seconds)

    try:
        work_task = asyncio.ensure_future(work)
        renewer = asyncio.create_task(renew_loop(), name=f"runtime-lease-renewer-{task_id}")
        done, _pending = await asyncio.wait({work_task, renewer}, return_when=asyncio.FIRST_COMPLETED)
        if renewer in done:
            renewal_error = renewer.exception()
            if fence.claim_finished.is_set():
                return await work_task
            if renewal_error is None:
                renewal_error = RuntimeError(f"RuntimeTask lease renewer stopped unexpectedly for {task_id}")
            work_task.cancel()
            try:
                await work_task
            except asyncio.CancelledError:
                pass
            raise renewal_error
        return await work_task
    finally:
        if renewer is not None and not renewer.done():
            renewer.cancel()
            try:
                await renewer
            except asyncio.CancelledError:
                pass
        if work_task is not None and not work_task.done():
            work_task.cancel()
            try:
                await work_task
            except asyncio.CancelledError:
                pass
        reset_runtime_task_fence(token)
