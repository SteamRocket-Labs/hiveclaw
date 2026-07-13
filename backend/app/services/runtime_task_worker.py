from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from loguru import logger

from app.config import get_settings
from app.database import async_session, enter_rls_bypass
from app.models.runtime_task import RuntimeTask
from app.services.budget_transition_outbox import BudgetTransitionOutboxService
from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
from app.services.runtime_task_claim_service import RuntimeTaskClaimService
from app.services.runtime_task_fence import run_claimed_runtime_task
from app.services.web_chat_runtime import active_web_chat_run_count, dispatch_web_chat_run, is_executable_chat_task_type


_LOCAL_WAKEUP_EVENT: asyncio.Event | None = None
_CLAIM_AND_DISPATCH_LOCK: asyncio.Lock | None = None
_CLAIM_AND_DISPATCH_LOCK_LOOP: asyncio.AbstractEventLoop | None = None
SUPPORTED_RUNTIME_TASK_TYPES = (
    "web_chat_turn",
    "goal_continuation",
    "team_member",
    "advanced_plan",
    "workflow",
    "delegation",
    "business_task",
    "subagent",
    "trigger",
    "heartbeat",
    "approval_execution",
    "hr_provisioning",
    "dream",
    "system_plan_run",
)
_DISPATCHED_TASKS: dict[str, tuple[str, asyncio.Task]] = {}
_STATE: dict[str, Any] = {
    "running": False,
    "worker_id": None,
    "last_claimed_at": None,
    "last_claimed_count": 0,
    "last_error": None,
    "wakeup_sent": 0,
    "wakeup_received": 0,
    "dispatched": 0,
    "expired_claims_reclaimed": 0,
    "outbox_claimed": 0,
    "outbox_reconciled": 0,
    "outbox_delivered": 0,
    "outbox_retried": 0,
    "outbox_deferred": 0,
    "outbox_dead_lettered": 0,
    "outbox_dead_letters_requeued": 0,
    "channel_outbox_claimed": 0,
    "channel_outbox_reconciled": 0,
    "channel_outbox_delivered": 0,
    "channel_outbox_retried": 0,
    "channel_outbox_dead_lettered": 0,
    "channel_outbox_needs_reconciliation": 0,
    "budget_outbox_claimed": 0,
    "budget_outbox_reconciled": 0,
    "budget_outbox_delivered": 0,
    "budget_outbox_retried": 0,
    "budget_outbox_dead_lettered": 0,
    "budget_outbox_needs_reconciliation": 0,
    "budget_reservations_reconciled": 0,
    "approval_execution_dispatched": 0,
    "hr_provisioning_dispatched": 0,
    "hr_drafts_reconciled": 0,
    "business_tasks_reconciled": 0,
    "dream_dispatched": 0,
    "system_plan_runs_dispatched": 0,
    "expired_inline_a2a_reconciled": 0,
    "inline_a2a_evidence_refreshed": 0,
    "workflow_recovery_resumed": 0,
    "workflow_live_evidence_repaired": 0,
    "workflow_activation_repaired": 0,
    "workflow_activation_failed": 0,
    "workflow_recovery_last_tick_monotonic": 0.0,
    "workflow_completion_outbox_delivered": 0,
    "workflow_completion_outbox_retried": 0,
    "workflow_completion_outbox_dead_lettered": 0,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _settings():
    return get_settings()


def _worker_id() -> str:
    settings = _settings()
    configured = str(settings.RUNTIME_TASK_WORKER_ID or "").strip()
    if configured:
        return configured
    return f"{socket.gethostname()}:{os.getpid()}"


def _wakeup_event() -> asyncio.Event:
    global _LOCAL_WAKEUP_EVENT
    if _LOCAL_WAKEUP_EVENT is None:
        _LOCAL_WAKEUP_EVENT = asyncio.Event()
    return _LOCAL_WAKEUP_EVENT


def _claim_and_dispatch_lock() -> asyncio.Lock:
    global _CLAIM_AND_DISPATCH_LOCK, _CLAIM_AND_DISPATCH_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _CLAIM_AND_DISPATCH_LOCK is None or _CLAIM_AND_DISPATCH_LOCK_LOOP is not loop:
        _CLAIM_AND_DISPATCH_LOCK = asyncio.Lock()
        _CLAIM_AND_DISPATCH_LOCK_LOOP = loop
    return _CLAIM_AND_DISPATCH_LOCK


def runtime_task_worker_enabled() -> bool:
    settings = _settings()
    role = str(settings.HIVE_PROCESS_ROLE or "runtime").strip().lower()
    return bool(settings.RUNTIME_TASK_WORKER_ENABLED) and role not in {"api", "read_model"}


def _parse_task_type_limits(raw_limits: str | None = None) -> dict[str, int]:
    raw = str(raw_limits if raw_limits is not None else _settings().RUNTIME_TASK_WORKER_TASK_TYPE_LIMITS or "")
    limits: dict[str, int] = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        task_type, value = item.split("=", 1)
        task_type = task_type.strip()
        if task_type not in SUPPORTED_RUNTIME_TASK_TYPES:
            continue
        try:
            limits[task_type] = max(0, int(value.strip()))
        except ValueError:
            continue
    return limits


def _cleanup_dispatched_tasks() -> None:
    done_keys = [run_id for run_id, (_task_type, task) in _DISPATCHED_TASKS.items() if task.done()]
    for run_id in done_keys:
        _DISPATCHED_TASKS.pop(run_id, None)


def _active_dispatched_task_type_counts() -> dict[str, int]:
    _cleanup_dispatched_tasks()
    counts: dict[str, int] = {}
    for task_type, _task in _DISPATCHED_TASKS.values():
        counts[task_type] = counts.get(task_type, 0) + 1
    return counts


def _total_active_runtime_task_count() -> int:
    return active_web_chat_run_count() + sum(_active_dispatched_task_type_counts().values())


def _task_type_capacity_remaining(task_type: str) -> int:
    limits = _parse_task_type_limits()
    limit = limits.get(task_type)
    if limit is None:
        return 0
    if task_type in {"web_chat_turn", "goal_continuation", "team_member", "advanced_plan"}:
        active = active_web_chat_run_count()
    else:
        active = _active_dispatched_task_type_counts().get(task_type, 0)
    return max(0, limit - active)


def _claimable_task_types_for_available_capacity() -> tuple[str, ...]:
    return tuple(
        task_type for task_type in SUPPORTED_RUNTIME_TASK_TYPES if _task_type_capacity_remaining(task_type) > 0
    )


def _task_type_capacities_for_available_slots() -> dict[str, int]:
    return {
        task_type: capacity
        for task_type in SUPPORTED_RUNTIME_TASK_TYPES
        if (capacity := _task_type_capacity_remaining(task_type)) > 0
    }


def _claim_batch_size_for_available_slots() -> int:
    settings = _settings()
    max_concurrent = max(1, int(settings.RUNTIME_TASK_WORKER_MAX_CONCURRENT))
    configured_batch = max(1, int(settings.RUNTIME_TASK_WORKER_BATCH_SIZE))
    available_slots = max(0, max_concurrent - _total_active_runtime_task_count())
    return min(configured_batch, available_slots)


async def notify_runtime_task_worker(*, reason: str, runtime_task_id: UUID | str | None = None) -> None:
    event = _wakeup_event()
    event.set()
    _STATE["wakeup_sent"] = int(_STATE.get("wakeup_sent") or 0) + 1
    payload = {
        "reason": reason,
        "runtime_task_id": str(runtime_task_id) if runtime_task_id else None,
        "created_at": _utcnow().isoformat(),
    }
    try:
        from app.core.events import get_redis

        redis = await get_redis()
        await redis.publish(_settings().RUNTIME_TASK_WAKEUP_CHANNEL, json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - local event + polling are fallback paths.
        logger.debug("[RuntimeTaskWorker] Redis wakeup publish failed: {}", exc)


async def _redis_wakeup_listener() -> None:
    try:
        from app.core.events import get_redis

        redis = await get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(_settings().RUNTIME_TASK_WAKEUP_CHANNEL)
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            _STATE["wakeup_received"] = int(_STATE.get("wakeup_received") or 0) + 1
            _wakeup_event().set()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - polling remains the fallback.
        _STATE["last_error"] = f"redis_listener:{type(exc).__name__}:{str(exc)[:300]}"
        logger.warning("[RuntimeTaskWorker] Redis wakeup listener stopped: {}", exc)


async def _claim_runtime_tasks_with_capacities(
    *,
    worker_id: str,
    task_type_capacities: dict[str, int],
    lease_seconds: int,
) -> list[RuntimeTask]:
    """Claim fairly across task types without exceeding any local type quota."""

    remaining_batch = _claim_batch_size_for_available_slots()
    if remaining_batch <= 0:
        return []
    remaining = {
        task_type: max(0, int(capacity))
        for task_type, capacity in task_type_capacities.items()
        if task_type in SUPPORTED_RUNTIME_TASK_TYPES and int(capacity) > 0
    }
    claimed: list[RuntimeTask] = []
    async with async_session() as db, enter_rls_bypass(db, reason="runtime task worker claim pending executable tasks"):
        while remaining_batch > 0 and remaining:
            claimed_this_round = False
            for task_type in tuple(remaining):
                if remaining_batch <= 0:
                    break
                service = RuntimeTaskClaimService(
                    db=db,
                    worker_id=worker_id,
                    task_types=(task_type,),
                    lease_seconds=lease_seconds,
                )
                task_batch = await service.claim_available(batch_size=1)
                if task_batch:
                    claimed.extend(task_batch)
                    remaining_batch -= len(task_batch)
                    remaining[task_type] -= len(task_batch)
                    claimed_this_round = True
                else:
                    remaining.pop(task_type, None)
                    continue
                if remaining.get(task_type, 0) <= 0:
                    remaining.pop(task_type, None)
            if not claimed_this_round:
                break
    return claimed


async def claim_and_dispatch_once(*, worker_id: str | None = None) -> list[str]:
    async with _claim_and_dispatch_lock():
        return await _claim_and_dispatch_once_locked(worker_id=worker_id)


async def _claim_and_dispatch_once_locked(*, worker_id: str | None = None) -> list[str]:
    settings = _settings()
    claimed_ids: list[str] = []
    batch_size = _claim_batch_size_for_available_slots()
    if batch_size <= 0:
        _STATE["last_claimed_count"] = 0
        return claimed_ids
    task_type_capacities = _task_type_capacities_for_available_slots()
    if not task_type_capacities:
        _STATE["last_claimed_count"] = 0
        return claimed_ids
    claimed = await _claim_runtime_tasks_with_capacities(
        worker_id=worker_id or _worker_id(),
        task_type_capacities=task_type_capacities,
        lease_seconds=settings.RUNTIME_TASK_CLAIM_LEASE_SECONDS,
    )

    reclaimed_count = sum(
        1 for task in claimed if bool((getattr(task, "metadata_json", None) or {}).get("reclaimed_expired_claim"))
    )
    if reclaimed_count:
        _STATE["expired_claims_reclaimed"] = int(_STATE.get("expired_claims_reclaimed") or 0) + reclaimed_count
        logger.warning(
            "[RuntimeTaskWorker] reclaimed {} expired runtime task claim(s) with new fences",
            reclaimed_count,
        )

    for task in claimed:
        if _dispatch_claimed_task(task):
            claimed_ids.append(task.id.hex)
    if claimed_ids:
        _STATE["last_claimed_at"] = _utcnow().isoformat()
        _STATE["last_claimed_count"] = len(claimed_ids)
        _STATE["dispatched"] = int(_STATE.get("dispatched") or 0) + len(claimed_ids)
    return claimed_ids


async def drain_runtime_notification_outbox_once(*, worker_id: str) -> dict[str, int]:
    service = RuntimeNotificationOutboxService()
    dead_letters_requeued = await service.retry_recoverable_dead_letters_once(limit=100)
    reconciled = await service.reconcile_terminal_tasks_once(limit=100)
    counts = await service.drain_once(worker_id=worker_id, limit=20)
    counts["reconciled"] = reconciled
    counts["dead_letters_requeued"] = dead_letters_requeued
    _STATE["outbox_reconciled"] = int(_STATE.get("outbox_reconciled") or 0) + reconciled
    _STATE["outbox_dead_letters_requeued"] = (
        int(_STATE.get("outbox_dead_letters_requeued") or 0) + dead_letters_requeued
    )
    for key in ("claimed", "delivered", "retried", "deferred", "dead_lettered"):
        state_key = f"outbox_{key}"
        _STATE[state_key] = int(_STATE.get(state_key) or 0) + int(counts.get(key) or 0)
    return counts


async def drain_channel_delivery_outbox_once(*, worker_id: str) -> dict[str, int]:
    service = ChannelDeliveryOutboxService()
    reconciled = await service.reconcile_workflow_terminal_runs_once(limit=100)
    counts = await service.drain_once(worker_id=worker_id, limit=20)
    counts["reconciled"] = reconciled
    _STATE["channel_outbox_reconciled"] = int(_STATE.get("channel_outbox_reconciled") or 0) + reconciled
    for key in ("claimed", "delivered", "retried", "dead_lettered", "needs_reconciliation"):
        state_key = f"channel_outbox_{key}"
        _STATE[state_key] = int(_STATE.get(state_key) or 0) + int(counts.get(key) or 0)
    return counts


async def drain_budget_transition_outbox_once(*, worker_id: str) -> dict[str, int]:
    service = BudgetTransitionOutboxService()
    reconciled = await service.reconcile_budget_events_once(limit=100)
    counts = await service.drain_once(worker_id=worker_id, limit=20)
    counts["reconciled"] = reconciled
    _STATE["budget_outbox_reconciled"] = int(_STATE.get("budget_outbox_reconciled") or 0) + reconciled
    for key in ("claimed", "delivered", "retried", "dead_lettered", "needs_reconciliation"):
        state_key = f"budget_outbox_{key}"
        _STATE[state_key] = int(_STATE.get(state_key) or 0) + int(counts.get(key) or 0)
    return counts


async def reconcile_runtime_budget_reservations_once(*, limit: int = 100) -> int:
    """Recover terminal usage receipts without depending on the optional daemon."""

    from app.services.runtime_budget_service import RuntimeBudgetService

    reconciled = await RuntimeBudgetService().reconcile_orphaned_reservations(limit=limit)
    _STATE["budget_reservations_reconciled"] = int(_STATE.get("budget_reservations_reconciled") or 0) + reconciled
    return reconciled


async def reconcile_hr_creation_drafts_once() -> dict[str, int]:
    from app.services.hr_creation_reconciliation import reconcile_hr_creation_drafts_once as reconcile

    summary = await reconcile()
    _STATE["hr_drafts_reconciled"] = int(_STATE.get("hr_drafts_reconciled") or 0) + int(summary.get("checked") or 0)
    return summary


async def reconcile_stale_business_tasks_once() -> dict[str, int]:
    from app.services.business_task_reconciliation import reconcile_stale_business_tasks_once as reconcile

    summary = await reconcile()
    _STATE["business_tasks_reconciled"] = int(_STATE.get("business_tasks_reconciled") or 0) + int(
        summary.get("quarantined") or 0
    )
    return summary


async def reconcile_expired_inline_a2a_once() -> int:
    """Quarantine only expired inline A2A leases; live replicas keep authority."""

    from app.services.runtime_task_service import (
        INLINE_A2A_RECOVERY_TASK_TYPES,
        reconcile_orphaned_runtime_tasks,
        refresh_inline_a2a_reconciliation_evidence,
    )

    reconciled = await reconcile_orphaned_runtime_tasks(
        task_types=set(INLINE_A2A_RECOVERY_TASK_TYPES),
        inline_a2a_only=True,
    )
    refreshed = await refresh_inline_a2a_reconciliation_evidence()
    _STATE["expired_inline_a2a_reconciled"] = int(_STATE.get("expired_inline_a2a_reconciled") or 0) + reconciled
    _STATE["inline_a2a_evidence_refreshed"] = int(_STATE.get("inline_a2a_evidence_refreshed") or 0) + refreshed
    return reconciled


async def reconcile_workflow_wakes_once(
    *,
    force: bool = False,
    service=None,
    leaf_executor=None,
) -> dict[str, int]:
    """Consume Workflow wake/recovery conditions from the always-on worker.

    ``CORE_DAEMON_STARTUP_ENABLED`` is false by default, so correctness cannot
    depend on the optional Workflow daemon. The interval throttle keeps this
    durable scan off the hot claim-poll path while ``force`` remains available
    for startup/tests/operator recovery.
    """

    settings = _settings()
    now = time.monotonic()
    interval = max(0.1, float(getattr(settings, "WORKFLOW_DAEMON_INTERVAL_SECONDS", 15)))
    last_tick = float(_STATE.get("workflow_recovery_last_tick_monotonic") or 0.0)
    if not force and now - last_tick < interval:
        return {"resumed_runs": 0, "signal_resumed_runs": 0, "subagent_woken_parents": 0}
    _STATE["workflow_recovery_last_tick_monotonic"] = now

    from app.services.workflow_daemon import get_default_workflow_service, workflow_daemon_tick
    from app.services.workflow_launch import build_resumable_workflow_leaf_executor

    runtime_service = service or get_default_workflow_service()
    activation_repair = await runtime_service.repair_pending_activations_once(limit=100)
    quota_repair = await runtime_service.repair_unsettled_quota_reservations_once(limit=100)
    repaired = await runtime_service.repair_pending_live_reconciliation_evidence(limit=100)
    result = await workflow_daemon_tick(
        service=runtime_service,
        leaf_executor=leaf_executor or build_resumable_workflow_leaf_executor(session_factory=async_session),
        session_factory=async_session,
    )
    resumed = int(result.get("resumed_runs") or 0) + int(result.get("signal_resumed_runs") or 0)
    _STATE["workflow_recovery_resumed"] = int(_STATE.get("workflow_recovery_resumed") or 0) + resumed
    result["live_evidence_repaired"] = len(repaired)
    result["activation_repaired"] = int(activation_repair.get("activated") or 0)
    result["activation_failed"] = int(activation_repair.get("failed") or 0)
    result["quota_reserved_released"] = int(quota_repair.get("settled_reserved") or 0)
    result["quota_executing_quarantined"] = int(quota_repair.get("quarantined_executing") or 0)
    _STATE["workflow_live_evidence_repaired"] = int(_STATE.get("workflow_live_evidence_repaired") or 0) + len(repaired)
    _STATE["workflow_activation_repaired"] = int(_STATE.get("workflow_activation_repaired") or 0) + int(
        activation_repair.get("activated") or 0
    )
    _STATE["workflow_activation_failed"] = int(_STATE.get("workflow_activation_failed") or 0) + int(
        activation_repair.get("failed") or 0
    )
    return result


async def drain_workflow_completion_outbox_once(*, worker_id: str) -> dict[str, int]:
    from app.services.workflow_completion_outbox import WorkflowCompletionOutboxService

    service = WorkflowCompletionOutboxService()
    reconciled = await service.reconcile_terminal_runs_once(limit=100)
    result = await service.drain_once(worker_id=worker_id, limit=20)
    result["reconciled"] = reconciled
    _STATE["workflow_completion_outbox_delivered"] = int(_STATE.get("workflow_completion_outbox_delivered") or 0) + int(
        result.get("delivered") or 0
    )
    _STATE["workflow_completion_outbox_retried"] = int(_STATE.get("workflow_completion_outbox_retried") or 0) + int(
        result.get("retried") or 0
    )
    _STATE["workflow_completion_outbox_dead_lettered"] = int(
        _STATE.get("workflow_completion_outbox_dead_lettered") or 0
    ) + int(result.get("dead_lettered") or 0)
    return result


def _dispatch_claimed_task(task: RuntimeTask) -> bool:
    if is_executable_chat_task_type(getattr(task, "task_type", None)):
        return dispatch_web_chat_run(
            task.id,
            claim_version=int(getattr(task, "claim_version", 0) or 0),
            worker_id=str(getattr(task, "claimed_by", None) or "unknown"),
        )
    if task.task_type == "workflow":
        return _dispatch_async_runtime_task(task, _execute_claimed_workflow_task(task.id), task_type="workflow")
    if task.task_type == "delegation":
        return _dispatch_async_runtime_task(task, _execute_claimed_delegation_task(task.id), task_type="delegation")
    if task.task_type == "business_task":
        return _dispatch_async_runtime_task(task, _execute_claimed_business_task(task.id), task_type="business_task")
    if task.task_type == "subagent":
        return _dispatch_async_runtime_task(task, _execute_claimed_subagent_task(task.id), task_type="subagent")
    if task.task_type == "trigger":
        return _dispatch_async_runtime_task(task, _execute_claimed_trigger_task(task.id), task_type="trigger")
    if task.task_type == "heartbeat":
        return _dispatch_async_runtime_task(task, _execute_claimed_heartbeat_task(task.id), task_type="heartbeat")
    if task.task_type == "approval_execution":
        dispatched = _dispatch_async_runtime_task(
            task,
            _execute_claimed_approval_execution_task(task.id),
            task_type="approval_execution",
        )
        if dispatched:
            _STATE["approval_execution_dispatched"] = int(_STATE.get("approval_execution_dispatched") or 0) + 1
        return dispatched
    if task.task_type == "hr_provisioning":
        dispatched = _dispatch_async_runtime_task(
            task,
            _execute_claimed_hr_provisioning_task(task.id),
            task_type="hr_provisioning",
        )
        if dispatched:
            _STATE["hr_provisioning_dispatched"] = int(_STATE.get("hr_provisioning_dispatched") or 0) + 1
        return dispatched
    if task.task_type == "dream":
        dispatched = _dispatch_async_runtime_task(
            task,
            _execute_claimed_dream_task(task.id),
            task_type="dream",
        )
        if dispatched:
            _STATE["dream_dispatched"] = int(_STATE.get("dream_dispatched") or 0) + 1
        return dispatched
    if task.task_type == "system_plan_run":
        dispatched = _dispatch_async_runtime_task(
            task,
            _execute_claimed_system_plan_task(task.id),
            task_type="system_plan_run",
            session_factory=async_session,
        )
        if dispatched:
            _STATE["system_plan_runs_dispatched"] = int(_STATE.get("system_plan_runs_dispatched") or 0) + 1
        return dispatched
    logger.warning("[RuntimeTaskWorker] Claimed unsupported task type {}; leaving task {}", task.task_type, task.id)
    return False


def _dispatch_async_runtime_task(
    task: RuntimeTask,
    coro,
    *,
    task_type: str,
    session_factory=None,
) -> bool:
    run_key = task.id.hex
    if run_key in _DISPATCHED_TASKS:
        return False
    async_task = asyncio.create_task(
        run_claimed_runtime_task(
            coro,
            task_id=task.id,
            claim_version=int(getattr(task, "claim_version", 0) or 0),
            worker_id=str(getattr(task, "claimed_by", None) or "unknown"),
            lease_seconds=float(_settings().RUNTIME_TASK_CLAIM_LEASE_SECONDS),
            session_factory=session_factory,
        ),
        name=f"runtime-{task_type}-{run_key}",
    )
    _DISPATCHED_TASKS[run_key] = (task_type, async_task)
    async_task.add_done_callback(lambda _task, run_id=run_key: _DISPATCHED_TASKS.pop(run_id, None))
    return True


async def _execute_claimed_workflow_task(run_id: UUID) -> None:
    try:
        from app.services.workflow_launch import execute_claimed_workflow_run

        await execute_claimed_workflow_run(run_id)
    except Exception as exc:  # noqa: BLE001 - worker loop must keep running.
        _STATE["last_error"] = f"workflow:{type(exc).__name__}:{str(exc)[:300]}"
        logger.exception("[RuntimeTaskWorker] workflow task {} failed", run_id)


async def _execute_claimed_hr_provisioning_task(task_id: UUID) -> None:
    try:
        from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning

        await execute_claimed_hr_provisioning(task_id)
    except Exception as exc:  # noqa: BLE001 - worker loop must keep running.
        _STATE["last_error"] = f"hr_provisioning:{type(exc).__name__}:{str(exc)[:300]}"
        logger.exception("[RuntimeTaskWorker] HR provisioning task {} failed", task_id)


async def _execute_claimed_dream_task(task_id: UUID) -> None:
    try:
        from app.services.dream_runtime import execute_claimed_dream

        await execute_claimed_dream(task_id)
    except Exception as exc:  # noqa: BLE001 - worker loop must keep running.
        _STATE["last_error"] = f"dream:{type(exc).__name__}:{str(exc)[:300]}"
        logger.exception("[RuntimeTaskWorker] Dream task {} failed", task_id)


async def _execute_claimed_system_plan_task(task_id: UUID) -> None:
    try:
        from app.services.plan_mode_system_run import execute_claimed_system_plan_run

        executed = await execute_claimed_system_plan_run(task_id, session_factory=async_session)
        if not executed:
            logger.warning("[RuntimeTaskWorker] System Plan task {} could not be executed", task_id)
    except Exception as exc:  # noqa: BLE001 - one invalid Plan must not stop the shared worker.
        _STATE["last_error"] = f"system_plan_run:{type(exc).__name__}:{str(exc)[:300]}"
        logger.exception("[RuntimeTaskWorker] System Plan task {} failed", task_id)


async def _execute_claimed_delegation_task(task_id: UUID) -> None:
    try:
        from app.agents.orchestrator import dispatch_persisted_async_delegation

        ok = await dispatch_persisted_async_delegation(task_id.hex)
        if not ok:
            logger.warning("[RuntimeTaskWorker] delegation task {} could not be dispatched", task_id)
    except Exception as exc:  # noqa: BLE001
        _STATE["last_error"] = f"delegation:{type(exc).__name__}:{str(exc)[:300]}"
        logger.exception("[RuntimeTaskWorker] delegation task {} failed", task_id)


async def _execute_claimed_subagent_task(task_id: UUID) -> None:
    try:
        from app.services.subagent_run_service import dispatch_persisted_subagent_run

        ok = await dispatch_persisted_subagent_run(task_id.hex)
        if not ok:
            logger.warning("[RuntimeTaskWorker] subagent task {} could not be dispatched", task_id)
    except Exception as exc:  # noqa: BLE001
        _STATE["last_error"] = f"subagent:{type(exc).__name__}:{str(exc)[:300]}"
        logger.exception("[RuntimeTaskWorker] subagent task {} failed", task_id)


async def _execute_claimed_trigger_task(task_id: UUID) -> None:
    try:
        from app.services.trigger_daemon import execute_claimed_trigger_runtime_task

        ok = await execute_claimed_trigger_runtime_task(task_id)
        if not ok:
            logger.warning("[RuntimeTaskWorker] trigger task {} could not be dispatched", task_id)
    except Exception as exc:  # noqa: BLE001
        _STATE["last_error"] = f"trigger:{type(exc).__name__}:{str(exc)[:300]}"
        logger.exception("[RuntimeTaskWorker] trigger task {} failed", task_id)


async def _execute_claimed_heartbeat_task(task_id: UUID) -> None:
    try:
        from app.services.heartbeat import execute_claimed_heartbeat_runtime_task

        ok = await execute_claimed_heartbeat_runtime_task(task_id)
        if not ok:
            logger.warning("[RuntimeTaskWorker] heartbeat task {} could not be dispatched", task_id)
    except Exception as exc:  # noqa: BLE001
        _STATE["last_error"] = f"heartbeat:{type(exc).__name__}:{str(exc)[:300]}"
        logger.exception("[RuntimeTaskWorker] heartbeat task {} failed", task_id)


async def _execute_claimed_approval_execution_task(task_id: UUID) -> None:
    try:
        from app.services.approval_execution_runtime import execute_claimed_approval_execution

        await execute_claimed_approval_execution(task_id)
    except Exception as exc:  # noqa: BLE001 - preserve uncertainty rather than replaying a side effect.
        _STATE["last_error"] = f"approval_execution:{type(exc).__name__}:{str(exc)[:300]}"
        logger.exception("[RuntimeTaskWorker] approval execution task {} failed", task_id)
        try:
            from app.services.runtime_task_service import update_runtime_task_record

            await update_runtime_task_record(
                task_id.hex,
                status="needs_reconciliation",
                result_summary=(
                    "Approval worker failed outside its persisted state machine; side effects require review: "
                    f"{type(exc).__name__}: {str(exc)[:500]}"
                ),
            )
        except Exception as persist_exc:  # noqa: BLE001
            logger.warning(
                "[RuntimeTaskWorker] failed to quarantine approval execution task {}: {}",
                task_id,
                persist_exc,
            )


async def _execute_claimed_business_task(runtime_task_id: UUID) -> None:
    try:
        from app.services.business_task_runtime import (
            BusinessTaskExecutionSuperseded,
            TaskExecutionOutcome,
            TaskExecutionStatus,
            finalize_business_task_execution,
            mark_business_task_execution_started,
        )
        from app.services.task_executor import (
            business_task_cancel_event,
            execute_task,
            release_business_task_cancel_event,
        )

        cancel_event = business_task_cancel_event(runtime_task_id)
        try:
            try:
                business_task_id, agent_id, requester_user_id = await mark_business_task_execution_started(
                    runtime_task_id=runtime_task_id
                )
            except BusinessTaskExecutionSuperseded:
                logger.info("[RuntimeTaskWorker] business task {} was cancelled before invocation", runtime_task_id)
                return
            try:
                outcome = await execute_task(
                    business_task_id,
                    agent_id,
                    requester_user_id=requester_user_id,
                    cancel_event=cancel_event,
                    runtime_task_id=runtime_task_id,
                )
            except Exception as exc:  # convert operational executor failure into the typed terminal contract.
                outcome = TaskExecutionOutcome(
                    status=TaskExecutionStatus.FAILED,
                    summary=f"Business task executor failed: {type(exc).__name__}: {str(exc)[:500]}",
                    error_code=type(exc).__name__,
                    retryable=True,
                )
            if not await finalize_business_task_execution(runtime_task_id=runtime_task_id, outcome=outcome):
                raise RuntimeError("business task finalization could not locate the claimed runtime task")
        finally:
            release_business_task_cancel_event(runtime_task_id, cancel_event)
    except Exception as exc:  # noqa: BLE001
        _STATE["last_error"] = f"business_task:{type(exc).__name__}:{str(exc)[:300]}"
        logger.exception("[RuntimeTaskWorker] business task {} failed", runtime_task_id)
        try:
            from app.services.runtime_task_service import update_runtime_task_record

            await update_runtime_task_record(
                runtime_task_id.hex,
                status="needs_reconciliation",
                result_summary=(
                    "business_task worker failed outside the atomic finalizer; side effects are unknown: "
                    f"{type(exc).__name__}: {str(exc)[:500]}"
                ),
            )
        except Exception as persist_exc:  # noqa: BLE001 - original failure is already logged.
            logger.warning(
                "[RuntimeTaskWorker] failed to persist failed status for business task {}: {}",
                runtime_task_id,
                persist_exc,
            )


async def start_runtime_task_worker_loop() -> None:
    if not runtime_task_worker_enabled():
        logger.info("[RuntimeTaskWorker] disabled for process role {}", _settings().HIVE_PROCESS_ROLE)
        return
    settings = _settings()
    worker_id = _worker_id()
    _STATE.update({"running": True, "worker_id": worker_id, "last_error": None})
    listener_task = asyncio.create_task(_redis_wakeup_listener(), name="runtime-task-worker-wakeup-listener")
    logger.info("[RuntimeTaskWorker] started worker_id={}", worker_id)
    try:
        while True:
            try:
                await reconcile_hr_creation_drafts_once()
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"hr_reconcile:{type(exc).__name__}: {str(exc)[:500]}"
                logger.exception("[RuntimeTaskWorker] HR draft reconciliation tick failed")
            try:
                await reconcile_stale_business_tasks_once()
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"business_task_reconcile:{type(exc).__name__}: {str(exc)[:500]}"
                logger.exception("[RuntimeTaskWorker] BusinessTask reconciliation tick failed")
            try:
                await reconcile_expired_inline_a2a_once()
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"a2a_reconcile:{type(exc).__name__}: {str(exc)[:500]}"
                logger.exception("[RuntimeTaskWorker] expired inline A2A reconciliation tick failed")
            try:
                await reconcile_workflow_wakes_once()
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"workflow_recovery:{type(exc).__name__}: {str(exc)[:500]}"
                logger.exception("[RuntimeTaskWorker] Workflow recovery tick failed")
            try:
                await drain_workflow_completion_outbox_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - task claiming must continue after one delivery failure.
                _STATE["last_error"] = f"workflow_completion_outbox:{type(exc).__name__}: {str(exc)[:500]}"
                logger.exception("[RuntimeTaskWorker] Workflow completion outbox tick failed")
            try:
                await drain_budget_transition_outbox_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - task claiming must continue after one delivery failure.
                _STATE["last_error"] = f"budget_outbox:{type(exc).__name__}: {str(exc)[:500]}"
                logger.exception("[RuntimeTaskWorker] budget transition outbox tick failed")
            try:
                await reconcile_runtime_budget_reservations_once()
            except Exception as exc:  # noqa: BLE001 - task claiming must survive one budget receipt failure.
                _STATE["last_error"] = f"budget_reconcile:{type(exc).__name__}: {str(exc)[:500]}"
                logger.exception("[RuntimeTaskWorker] runtime budget reservation reconciliation tick failed")
            try:
                await drain_channel_delivery_outbox_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - task claiming must continue after one delivery failure.
                _STATE["last_error"] = f"channel_outbox:{type(exc).__name__}: {str(exc)[:500]}"
                logger.exception("[RuntimeTaskWorker] channel delivery outbox tick failed")
            try:
                await drain_runtime_notification_outbox_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - task claiming must continue after one outbox failure.
                _STATE["last_error"] = f"outbox:{type(exc).__name__}: {str(exc)[:500]}"
                logger.exception("[RuntimeTaskWorker] completion outbox tick failed")
            try:
                await claim_and_dispatch_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - worker loop must survive one bad claim.
                _STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
                logger.exception("[RuntimeTaskWorker] claim/dispatch tick failed")
            event = _wakeup_event()
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=float(settings.RUNTIME_TASK_CLAIM_POLL_SECONDS))
            except asyncio.TimeoutError:
                pass
    finally:
        _STATE["running"] = False
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass


def runtime_task_worker_snapshot() -> dict[str, Any]:
    return dict(_STATE)
