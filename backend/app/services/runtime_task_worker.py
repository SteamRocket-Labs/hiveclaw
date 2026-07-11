from __future__ import annotations

import asyncio
import json
import os
import socket
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
    "outbox_claimed": 0,
    "outbox_reconciled": 0,
    "outbox_delivered": 0,
    "outbox_retried": 0,
    "outbox_deferred": 0,
    "outbox_dead_lettered": 0,
    "channel_outbox_claimed": 0,
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


async def claim_and_dispatch_once(*, worker_id: str | None = None) -> list[str]:
    settings = _settings()
    claimed_ids: list[str] = []
    batch_size = _claim_batch_size_for_available_slots()
    if batch_size <= 0:
        _STATE["last_claimed_count"] = 0
        return claimed_ids
    claimable_task_types = _claimable_task_types_for_available_capacity()
    if not claimable_task_types:
        _STATE["last_claimed_count"] = 0
        return claimed_ids
    async with async_session() as db, enter_rls_bypass(db, reason="runtime task worker claim pending executable tasks"):
        service = RuntimeTaskClaimService(
            db=db,
            worker_id=worker_id or _worker_id(),
            task_types=claimable_task_types,
            lease_seconds=settings.RUNTIME_TASK_CLAIM_LEASE_SECONDS,
        )
        claimed = await service.claim_available(batch_size=batch_size)

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
    reconciled = await service.reconcile_terminal_tasks_once(limit=100)
    counts = await service.drain_once(worker_id=worker_id, limit=20)
    counts["reconciled"] = reconciled
    _STATE["outbox_reconciled"] = int(_STATE.get("outbox_reconciled") or 0) + reconciled
    for key in ("claimed", "delivered", "retried", "deferred", "dead_lettered"):
        state_key = f"outbox_{key}"
        _STATE[state_key] = int(_STATE.get(state_key) or 0) + int(counts.get(key) or 0)
    return counts


async def drain_channel_delivery_outbox_once(*, worker_id: str) -> dict[str, int]:
    counts = await ChannelDeliveryOutboxService().drain_once(worker_id=worker_id, limit=20)
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
    logger.warning("[RuntimeTaskWorker] Claimed unsupported task type {}; leaving task {}", task.task_type, task.id)
    return False


def _dispatch_async_runtime_task(task: RuntimeTask, coro, *, task_type: str) -> bool:
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


async def _execute_claimed_business_task(runtime_task_id: UUID) -> None:
    try:
        from app.services.business_task_runtime import (
            TaskExecutionOutcome,
            TaskExecutionStatus,
            finalize_business_task_execution,
            mark_business_task_execution_started,
        )
        from app.services.task_executor import execute_task

        business_task_id, agent_id, requester_user_id = await mark_business_task_execution_started(
            runtime_task_id=runtime_task_id
        )
        try:
            outcome = await execute_task(
                business_task_id,
                agent_id,
                requester_user_id=requester_user_id,
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
                await drain_budget_transition_outbox_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - task claiming must continue after one delivery failure.
                _STATE["last_error"] = f"budget_outbox:{type(exc).__name__}: {str(exc)[:500]}"
                logger.exception("[RuntimeTaskWorker] budget transition outbox tick failed")
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
