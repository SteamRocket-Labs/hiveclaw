"""Durable ownership and recovery for full and soft Dream cycles."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from loguru import logger
from sqlalchemy import select

from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.services.runtime_task_fence import assert_runtime_task_fence, renew_current_runtime_task_lease


DreamMode = Literal["full", "soft"]
DREAM_RUNTIME_SCHEMA = "dream_runtime_task.v1"
_MAX_SAFE_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class DreamEnqueueResult:
    task_id: uuid.UUID
    created: bool
    mode: DreamMode
    idempotency_key: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _snapshot_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dream_identity(
    *,
    agent_id: uuid.UUID,
    mode: DreamMode,
    state: dict[str, Any],
    now: datetime,
) -> tuple[str, int]:
    current_version = int(state.get("version") or 0)
    expected_version = current_version + 1 if mode == "full" else current_version
    if mode == "full":
        return f"dream:{agent_id}:v{expected_version}", expected_version
    six_hour_window = int(now.timestamp() // (6 * 60 * 60))
    return f"soft-dream:{agent_id}:v{current_version}:w{six_hour_window}", expected_version


def _build_dream_task(
    *,
    agent: Agent,
    mode: DreamMode,
    source: str,
    state: dict[str, Any],
    recovery_source: str | None,
    now: datetime,
) -> RuntimeTask:
    root_key, expected_version = _dream_identity(
        agent_id=agent.id,
        mode=mode,
        state=state,
        now=now,
    )
    metadata = {
        "schema": DREAM_RUNTIME_SCHEMA,
        "dream_mode": mode,
        "source": source,
        "recovery_source": recovery_source,
        "phase": "queued",
        "expected_dream_version": expected_version,
        "state_snapshot": state,
        "replay_contract": "idempotent_agent_asset_transaction_and_dream_state_version",
        "side_effect_risk": "agent_internal_idempotent",
        "automatic_retry_allowed": True,
        "queued_at": now.isoformat(),
    }
    config = {
        "task_type": "dream",
        "agent_id": str(agent.id),
        "tenant_id": str(agent.tenant_id),
        "mode": mode,
        "expected_dream_version": expected_version,
    }
    policy = {
        "tenant_id": str(agent.tenant_id),
        "owner_user_id": str(agent.owner_user_id or agent.creator_id)
        if (agent.owner_user_id or agent.creator_id)
        else None,
        "internal_only": True,
    }
    return RuntimeTask(
        id=uuid.uuid4(),
        task_type="dream",
        parent_agent_id=agent.id,
        tenant_id=agent.tenant_id,
        status="pending",
        scheduled_at=now,
        priority=12,
        prompt=f"Run {mode} governed Dream consolidation.",
        trace_id=root_key,
        root_user_id=agent.owner_user_id or agent.creator_id,
        delegation_chain_json=[f"agent:{agent.id}"],
        depth=1,
        root_idempotency_key=root_key,
        config_snapshot_hash=_snapshot_hash(config),
        policy_snapshot_hash=_snapshot_hash(policy),
        metadata_json=metadata,
    )


async def enqueue_dream_runtime_task(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    mode: DreamMode,
    source: str,
    recovery_source: str | None = None,
) -> DreamEnqueueResult:
    """Serialize per-agent cadence admission and create at most one job."""

    import app.database as database
    from app.services.auto_dream import dream_state_snapshot

    now = _utcnow()
    state = dream_state_snapshot(agent_id)
    root_key, _expected_version = _dream_identity(
        agent_id=agent_id,
        mode=mode,
        state=state,
        now=now,
    )
    created_task: RuntimeTask | None = None
    async with database.tenant_scoped_session(
        tenant_id,
        session_factory=database.async_session,
        require_tenant=True,
        source="dream_runtime_enqueue",
    ) as db:
        agent = (
            await db.execute(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id).with_for_update())
        ).scalar_one_or_none()
        if agent is None:
            raise ValueError("Dream agent is missing or outside the tenant authority")
        existing = (
            await db.execute(
                select(RuntimeTask).where(
                    RuntimeTask.root_idempotency_key == root_key,
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.parent_agent_id == agent_id,
                    RuntimeTask.task_type == "dream",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return DreamEnqueueResult(existing.id, False, mode, root_key)
        created_task = _build_dream_task(
            agent=agent,
            mode=mode,
            source=source,
            state=state,
            recovery_source=recovery_source,
            now=now,
        )
        db.add(created_task)
        await db.flush()
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="memory.dream_queued",
            severity="info",
            actor_type="agent",
            actor_id=agent.id,
            tenant_id=tenant_id,
            action="enqueue_dream_runtime_task",
            resource_type="runtime_task",
            resource_id=created_task.id,
            details={
                "dream_mode": mode,
                "source": source,
                "root_idempotency_key": root_key,
                "recovery_source": recovery_source,
            },
        )

    from app.services.runtime_task_worker import notify_runtime_task_worker

    await notify_runtime_task_worker(reason="dream_queued", runtime_task_id=created_task.id)
    return DreamEnqueueResult(created_task.id, True, mode, root_key)


async def enqueue_due_dream(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    source: str,
    recovery_source: str | None = None,
) -> DreamEnqueueResult | None:
    """Select the due lane once; full Dream always takes precedence."""

    from app.services.auto_dream import should_dream, should_soft_dream

    mode: DreamMode | None = None
    if should_dream(agent_id):
        mode = "full"
    elif should_soft_dream(agent_id):
        mode = "soft"
    if mode is None:
        return None
    return await enqueue_dream_runtime_task(
        agent_id=agent_id,
        tenant_id=tenant_id,
        mode=mode,
        source=source,
        recovery_source=recovery_source,
    )


async def reconcile_due_dream_runtime_tasks(*, limit: int = 200) -> dict[str, int]:
    """Backfill file-backed due state that has no RuntimeTask owner.

    The legacy cadence truth lives in per-agent control files, so SQL migration
    cannot inspect it. This periodic, idempotent scanner is the executable
    backfill and crash-window repair path.
    """

    import app.database as database
    from app.services.auto_dream import list_dream_state_agent_ids
    from app.services.tenant_resolver import resolve_tenant_for_agent

    result = {"scanned": 0, "queued": 0, "existing": 0, "failed": 0}
    for agent_id in list_dream_state_agent_ids():
        result["scanned"] += 1
        try:
            tenant_id = await resolve_tenant_for_agent(agent_id, session_factory=database.async_session)
            if tenant_id is None:
                raise ValueError("Dream state has no tenant-bound Agent owner")
            queued = await enqueue_due_dream(
                agent_id=agent_id,
                tenant_id=tenant_id,
                source="reconciler",
                recovery_source="due_state_reconciler",
            )
            if queued is None:
                continue
            result["queued" if queued.created else "existing"] += 1
            if result["queued"] >= max(1, int(limit)):
                break
        except Exception as exc:  # Continue repairing other tenant-bound agents.
            result["failed"] += 1
            logger.warning("[DreamRuntime] due-state reconciliation failed for {}: {}", agent_id, exc)
    return result


def _clear_claim(task: RuntimeTask) -> None:
    task.claimed_by = None
    task.claim_expires_at = None


def _complete_task(
    task: RuntimeTask,
    *,
    outcome: dict[str, Any],
    recovered_from_state: bool = False,
) -> str:
    now = _utcnow()
    metadata = dict(task.metadata_json or {})
    normalized_outcome = {"status": "completed", **dict(outcome or {})}
    task.status = "completed"
    task.completed_at = task.completed_at or now
    task.result_summary = json.dumps(normalized_outcome, ensure_ascii=False, default=str)[:20_000]
    _clear_claim(task)
    metadata.update(
        {
            "phase": "terminal",
            "terminal_at": now.isoformat(),
            "automatic_retry_allowed": False,
            "recovered_from_state": recovered_from_state,
            "outcome": normalized_outcome,
        }
    )
    task.metadata_json = metadata
    return "completed"


async def _load_tenant_id(task_id: uuid.UUID) -> uuid.UUID | None:
    import app.database as database
    from app.services.tenant_resolver import resolve_tenant_for_runtime_task

    return await resolve_tenant_for_runtime_task(task_id, session_factory=database.async_session)


async def _prepare_dream(
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> tuple[str, uuid.UUID | None, DreamMode | None]:
    import app.database as database
    from app.services.auto_dream import dream_state_snapshot

    async with database.tenant_scoped_session(
        tenant_id,
        session_factory=database.async_session,
        require_tenant=True,
        source="dream_runtime_prepare",
    ) as db:
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        if task is None:
            return "missing", None, None
        assert_runtime_task_fence(task)
        metadata = dict(task.metadata_json or {})
        mode = str(metadata.get("dream_mode") or "full")
        if mode not in {"full", "soft"} or task.parent_agent_id is None:
            task.status = "needs_reconciliation"
            task.completed_at = _utcnow()
            task.result_summary = "Dream task has invalid mode or agent authority."
            _clear_claim(task)
            metadata.update(
                {
                    "phase": "terminal",
                    "needs_reconciliation": True,
                    "automatic_retry_allowed": False,
                }
            )
            task.metadata_json = metadata
            return "needs_reconciliation", task.parent_agent_id, None
        if task.status == "completed":
            return "completed", task.parent_agent_id, mode
        expected_version = int(metadata.get("expected_dream_version") or 0)
        if mode == "full" and int(dream_state_snapshot(task.parent_agent_id).get("version") or 0) >= expected_version:
            return (
                _complete_task(
                    task,
                    outcome={"dream_version": expected_version, "recovery": "state_already_advanced"},
                    recovered_from_state=True,
                ),
                task.parent_agent_id,
                mode,
            )
        if task.status != "running":
            return str(task.status or "not_claimed"), task.parent_agent_id, mode
        metadata.update(
            {
                "phase": "executing",
                "execution_started_at": _utcnow().isoformat(),
                "automatic_retry_allowed": False,
            }
        )
        task.metadata_json = metadata
        return "execute", task.parent_agent_id, mode


async def _run_domain_dream(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    mode: DreamMode,
) -> dict[str, Any]:
    from app.services.auto_dream import run_dream, run_soft_dream

    if mode == "soft":
        return await run_soft_dream(agent_id)
    return await run_dream(agent_id, tenant_id)


async def _finalize_dream(
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    result: dict[str, Any] | None,
    error: str | None,
) -> str:
    import app.database as database

    async with database.tenant_scoped_session(
        tenant_id,
        session_factory=database.async_session,
        require_tenant=True,
        source="dream_runtime_finalize",
    ) as db:
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        if task is None:
            return "missing"
        assert_runtime_task_fence(task)
        if task.status == "completed":
            return "completed"
        if error is None:
            return _complete_task(task, outcome=dict(result or {}))
        metadata = dict(task.metadata_json or {})
        attempts = int(task.attempt_count or 0)
        if attempts < _MAX_SAFE_ATTEMPTS:
            task.status = "resumable"
            task.scheduled_at = _utcnow() + timedelta(seconds=max(5, attempts * 10))
            task.completed_at = None
            task.result_summary = error[:20_000]
            _clear_claim(task)
            metadata.update(
                {
                    "phase": "retry_wait",
                    "retry_reason": error[:2_000],
                    "automatic_retry_allowed": True,
                    "last_attempt_outcome": dict(result or {}),
                }
            )
            task.metadata_json = metadata
            return "retry_scheduled"
        task.status = "needs_reconciliation"
        task.completed_at = _utcnow()
        task.result_summary = error[:20_000]
        _clear_claim(task)
        metadata.update(
            {
                "phase": "terminal",
                "automatic_retry_allowed": False,
                "needs_reconciliation": True,
                "reconciliation_status": "open",
                "reconciliation_reason": "dream_retry_exhausted",
                "reconciliation_retry_allowed": True,
                "outcome": {"status": "needs_reconciliation", "error": error[:2_000]},
                "last_attempt_outcome": dict(result or {}),
            }
        )
        task.metadata_json = metadata
        return "needs_reconciliation"


async def execute_claimed_dream(task_id: uuid.UUID) -> str:
    """Consume one claimed Dream job with state-version crash convergence."""

    tenant_id = await _load_tenant_id(task_id)
    if tenant_id is None:
        logger.error("[DreamRuntime] RuntimeTask {} has no tenant authority", task_id)
        return "missing_tenant"
    disposition, agent_id, mode = await _prepare_dream(task_id, tenant_id)
    if disposition != "execute":
        return disposition
    if agent_id is None or mode is None:
        return "needs_reconciliation"
    await renew_current_runtime_task_lease(lease_seconds=900.0)
    result: dict[str, Any] | None = None
    error: str | None = None
    try:
        result = await _run_domain_dream(agent_id=agent_id, tenant_id=tenant_id, mode=mode)
        if str(result.get("status") or "") == "degraded":
            error = str(result.get("reason") or "dream_semantic_degraded")
    except Exception as exc:  # The next claim re-enters only idempotent internal writes.
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("[DreamRuntime] task {} failed before terminal commit", task_id)
    return await _finalize_dream(task_id, tenant_id, result=result, error=error)
