from __future__ import annotations

import asyncio
import json
import os
import socket
from datetime import datetime, timedelta, timezone
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
    "a2a_continuation",
    "workflow",
    "delegation",
    "business_task",
    "subagent",
    "trigger",
    "approval_execution",
    "hr_provisioning",
    "dream",
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
    "approval_execution_dispatched": 0,
    "hr_provisioning_dispatched": 0,
    "hr_drafts_reconciled": 0,
    "business_tasks_reconciled": 0,
    "session_controls_claimed": 0,
    "session_controls_started": 0,
    "session_controls_signalled": 0,
    "session_controls_settled": 0,
    "session_permissions_expired": 0,
    "session_model_rounds_committed": 0,
    "session_model_rounds_needs_reconciliation": 0,
    "session_terminal_outcomes_committed": 0,
    "session_terminal_outcomes_needs_reconciliation": 0,
    "session_terminal_candidates_committed": 0,
    "session_terminal_candidates_held": 0,
    "turn_replacements_claimed": 0,
    "turn_replacements_transitioned": 0,
    "turn_replacements_signalled": 0,
    "turn_replacements_started_runs": 0,
    "turn_replacements_completed": 0,
    "turn_replacements_retryable_failures": 0,
    "turn_replacements_needs_reconciliation": 0,
    "session_event_outbox_claimed": 0,
    "session_event_outbox_published": 0,
    "session_event_outbox_retried": 0,
    "session_event_outbox_dead_lettered": 0,
    "input_admissions_claimed": 0,
    "input_admissions_recovered": 0,
    "input_admissions_needs_reconciliation": 0,
    "session_input_dispatches_claimed": 0,
    "session_input_dispatches_dispatched": 0,
    "session_input_dispatches_deferred": 0,
    "session_input_dispatches_retried": 0,
    "session_input_rollovers_claimed": 0,
    "session_input_rollovers_dispatched": 0,
    "session_input_rollovers_deferred": 0,
    "session_input_rollovers_retried": 0,
    "team_fanout_claimed": 0,
    "team_fanout_recovered": 0,
    "team_fanout_retried": 0,
    "team_fanout_needs_reconciliation": 0,
    "dream_dispatched": 0,
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
    if task_type in {"web_chat_turn", "goal_continuation", "team_member", "advanced_plan", "a2a_continuation"}:
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
        _STATE["last_error"] = f"redis_listener:{type(exc).__name__}:{exc}"
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

    reclaimed_count = sum(
        1 for task in claimed if bool((getattr(task, "metadata_json", None) or {}).get("reclaimed_expired_claim"))
    )
    if reclaimed_count:
        _STATE["expired_claims_reclaimed"] = int(_STATE.get("expired_claims_reclaimed") or 0) + reclaimed_count
        logger.warning(
            "[RuntimeTaskWorker] reclaimed {} expired web-chat claim(s) with new fences",
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
    reconciled = await service.reconcile_terminal_tasks_once(limit=100)
    counts = await service.drain_once(worker_id=worker_id, limit=100)
    counts["reconciled"] = reconciled
    _STATE["outbox_reconciled"] = int(_STATE.get("outbox_reconciled") or 0) + reconciled
    for key in ("claimed", "delivered", "retried", "deferred", "dead_lettered"):
        state_key = f"outbox_{key}"
        _STATE[state_key] = int(_STATE.get(state_key) or 0) + int(counts.get(key) or 0)
    if str(_STATE.get("last_error") or "").startswith("outbox:"):
        _STATE["last_error"] = None
    return counts


async def drain_session_event_outbox_once(
    *,
    worker_id: str,
    publish_callback=None,
    session_factory=async_session,
) -> dict[str, int]:
    from app.services.session_event_outbox import SessionEventOutboxPublisher

    service = SessionEventOutboxPublisher(session_factory=session_factory)
    counts = await service.drain_once(
        worker_id=worker_id,
        publish_callback=publish_callback,
    )
    for key in ("claimed", "published", "retried", "dead_lettered"):
        state_key = f"session_event_outbox_{key}"
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


async def recover_team_fanout_admissions_once(*, worker_id: str) -> dict[str, int]:
    """Recover complete Team requested sets left between reserve/enqueue steps."""

    from app.services.team_fanout_recovery import TeamFanoutRecoveryService

    counts = await TeamFanoutRecoveryService().drain_once(worker_id=worker_id, limit=100)
    for key in ("claimed", "recovered", "retried", "needs_reconciliation"):
        state_key = f"team_fanout_{key}"
        _STATE[state_key] = int(_STATE.get(state_key) or 0) + int(counts.get(key) or 0)
    return counts


async def recover_session_control_inputs_once(
    *,
    worker_id: str,
    signal_callback=None,
    stale_after: timedelta = timedelta(seconds=5),
    tenant_id: UUID | None = None,
    session_factory=async_session,
) -> dict[str, int]:
    """Run the cross-tenant control recovery lane under audited RLS bypass."""

    from app.services.session_control_input import recover_stale_cancel_control_inputs_once

    async with (
        session_factory() as db,
        enter_rls_bypass(
            db,
            reason="runtime task worker recover stale Session V2 control inputs",
            actor_id=worker_id,
        ),
    ):
        counts = await recover_stale_cancel_control_inputs_once(
            db,
            worker_id=worker_id,
            signal_callback=signal_callback,
            stale_after=stale_after,
            tenant_id=tenant_id,
        )
    for key in ("claimed", "started", "signalled", "settled"):
        state_key = f"session_controls_{key}"
        _STATE[state_key] = int(_STATE.get(state_key) or 0) + int(counts.get(key) or 0)
    return counts


async def expire_session_permission_requests_once(
    *,
    worker_id: str,
    session_factory=async_session,
) -> int:
    """Settle expired permission waits while the runtime remains online."""

    from app.services.session_permission_runtime import expire_stale_session_permission_requests

    async with (
        session_factory() as db,
        enter_rls_bypass(
            db,
            reason="runtime task worker expire Session V2 permission requests",
            actor_id=worker_id,
        ),
    ):
        expired = await expire_stale_session_permission_requests(db=db)
    _STATE["session_permissions_expired"] = int(_STATE.get("session_permissions_expired") or 0) + expired
    return expired


async def recover_session_model_rounds_once(
    *,
    worker_id: str,
    limit: int = 50,
    run_id: UUID | None = None,
    session_factory=async_session,
) -> dict[str, int]:
    """Commit durable ModelResult seals without replaying Provider requests."""

    from app.services.session_model_round import recover_sealed_model_rounds_once

    async with (
        session_factory() as db,
        enter_rls_bypass(
            db,
            reason="runtime task worker recover sealed Session V2 model rounds",
            actor_id=worker_id,
        ),
    ):
        counts = await recover_sealed_model_rounds_once(
            db,
            worker_id=worker_id,
            limit=limit,
            run_id=run_id,
        )
        await db.commit()
    _STATE["session_model_rounds_committed"] = int(_STATE.get("session_model_rounds_committed") or 0) + int(
        counts.get("round_committed") or 0
    )
    _STATE["session_model_rounds_needs_reconciliation"] = int(
        _STATE.get("session_model_rounds_needs_reconciliation") or 0
    ) + int(counts.get("needs_reconciliation") or 0)
    return counts


async def recover_session_terminal_outcomes_once(
    *,
    worker_id: str,
    limit: int = 50,
    run_id: UUID | None = None,
    session_factory=async_session,
) -> dict[str, int]:
    """Recover sealed outcomes, then eligible results, using stable identities."""

    from app.services.session_terminal_outcome import (
        recover_terminal_candidates_once,
        recover_terminal_outcomes_once,
    )

    async with (
        session_factory() as db,
        enter_rls_bypass(
            db,
            reason="runtime task worker recover sealed Session V2 terminal outcomes",
            actor_id=worker_id,
        ),
    ):
        sealed = await recover_terminal_outcomes_once(
            db,
            worker_id=worker_id,
            limit=limit,
            run_id=run_id,
        )
        await db.commit()
    async with (
        session_factory() as db,
        enter_rls_bypass(
            db,
            reason="runtime task worker recover Session V2 terminal candidates",
            actor_id=worker_id,
        ),
    ):
        candidates = await recover_terminal_candidates_once(
            db,
            worker_id=worker_id,
            limit=limit,
            run_id=run_id,
        )
        await db.commit()
    _STATE["session_terminal_outcomes_committed"] = int(_STATE.get("session_terminal_outcomes_committed") or 0) + int(
        sealed.get("terminal_committed") or 0
    )
    _STATE["session_terminal_outcomes_needs_reconciliation"] = int(
        _STATE.get("session_terminal_outcomes_needs_reconciliation") or 0
    ) + int(sealed.get("needs_reconciliation") or 0)
    _STATE["session_terminal_candidates_committed"] = int(
        _STATE.get("session_terminal_candidates_committed") or 0
    ) + int(candidates.get("terminal_committed") or 0)
    _STATE["session_terminal_candidates_held"] = int(_STATE.get("session_terminal_candidates_held") or 0) + int(
        candidates.get("held") or 0
    )
    return {
        "sealed_terminal_committed": int(sealed.get("terminal_committed") or 0),
        "sealed_needs_reconciliation": int(sealed.get("needs_reconciliation") or 0),
        "candidate_terminal_committed": int(candidates.get("terminal_committed") or 0),
        "candidate_held": int(candidates.get("held") or 0),
    }


async def recover_turn_replacement_sagas_once(
    *,
    worker_id: str,
    signal_callback=None,
    start_replacement_callback=None,
    stale_after: timedelta = timedelta(seconds=5),
    tenant_id: UUID | None = None,
    session_factory=async_session,
) -> dict[str, int]:
    """Advance durable replacement sagas under the worker's audited RLS lane."""

    from app.services.session_turn_replacement import recover_turn_replacements_once

    async with (
        session_factory() as db,
        enter_rls_bypass(
            db,
            reason="runtime task worker recover Session V2 turn replacement sagas",
            actor_id=worker_id,
        ),
    ):
        counts = await recover_turn_replacements_once(
            db,
            worker_id=worker_id,
            signal_callback=signal_callback,
            start_replacement_callback=start_replacement_callback,
            stale_after=stale_after,
            tenant_id=tenant_id,
        )
    for key in (
        "claimed",
        "transitioned",
        "signalled",
        "started_runs",
        "completed",
        "retryable_failures",
        "needs_reconciliation",
    ):
        state_key = f"turn_replacements_{key}"
        _STATE[state_key] = int(_STATE.get(state_key) or 0) + int(counts.get(key) or 0)
    return counts


async def recover_stale_session_input_admissions_once(
    *,
    worker_id: str,
    managed_result_lookup=None,
    stale_after: timedelta = timedelta(minutes=2),
    tenant_id: UUID | None = None,
    session_factory=async_session,
) -> dict[str, int]:
    """Recover stale current-revision admission attempts under audited RLS."""

    from app.services.session_input_admission import recover_stale_input_admissions_once

    async with (
        session_factory() as db,
        enter_rls_bypass(
            db,
            reason="runtime task worker recover stale Session V2 input admissions",
            actor_id=worker_id,
        ),
    ):
        counts = await recover_stale_input_admissions_once(
            db,
            worker_id=worker_id,
            managed_result_lookup=managed_result_lookup,
            stale_after=stale_after,
            tenant_id=tenant_id,
        )
    for key in ("claimed", "recovered", "needs_reconciliation"):
        state_key = f"input_admissions_{key}"
        _STATE[state_key] = int(_STATE.get(state_key) or 0) + int(counts.get(key) or 0)
    return counts


async def recover_session_input_dispatches_once(
    *,
    worker_id: str,
    stale_after: timedelta = timedelta(seconds=5),
    tenant_id: UUID | None = None,
    session_factory=async_session,
) -> dict[str, int]:
    """Dispatch every admitted HumanInput from the durable database lane."""

    from app.services.session_input_dispatch import recover_admitted_session_inputs_once

    async with (
        session_factory() as db,
        enter_rls_bypass(
            db,
            reason="runtime task worker dispatch admitted Session V2 inputs",
            actor_id=worker_id,
        ),
    ):
        counts = await recover_admitted_session_inputs_once(
            db,
            worker_id=worker_id,
            stale_after=stale_after,
            tenant_id=tenant_id,
        )
    for key in ("claimed", "dispatched", "deferred", "retried"):
        state_key = f"session_input_dispatches_{key}"
        _STATE[state_key] = int(_STATE.get(state_key) or 0) + int(counts.get(key) or 0)
    return counts


async def recover_terminal_target_session_inputs_once(
    *,
    worker_id: str,
    stale_after: timedelta = timedelta(seconds=5),
    tenant_id: UUID | None = None,
    session_factory=async_session,
) -> dict[str, int]:
    """Roll over dispatched steers whose target run terminalized after mailing."""

    from app.services.session_input_dispatch import recover_dispatched_terminal_steers_once

    async with (
        session_factory() as db,
        enter_rls_bypass(
            db,
            reason="runtime task worker roll over Session V2 steers after target run terminal",
            actor_id=worker_id,
        ),
    ):
        counts = await recover_dispatched_terminal_steers_once(
            db,
            worker_id=worker_id,
            stale_after=stale_after,
            tenant_id=tenant_id,
        )
    for key in ("claimed", "dispatched", "deferred", "retried"):
        state_key = f"session_input_rollovers_{key}"
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
        _STATE["last_error"] = f"workflow:{type(exc).__name__}:{exc}"
        logger.exception("[RuntimeTaskWorker] workflow task {} failed", run_id)


async def _execute_claimed_hr_provisioning_task(task_id: UUID) -> None:
    try:
        from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning

        await execute_claimed_hr_provisioning(task_id)
    except Exception as exc:  # noqa: BLE001 - worker loop must keep running.
        _STATE["last_error"] = f"hr_provisioning:{type(exc).__name__}:{exc}"
        logger.exception("[RuntimeTaskWorker] HR provisioning task {} failed", task_id)


async def _execute_claimed_dream_task(task_id: UUID) -> None:
    try:
        from app.services.dream_runtime import execute_claimed_dream

        await execute_claimed_dream(task_id)
    except Exception as exc:  # noqa: BLE001 - worker loop must keep running.
        _STATE["last_error"] = f"dream:{type(exc).__name__}:{exc}"
        logger.exception("[RuntimeTaskWorker] Dream task {} failed", task_id)


async def _execute_claimed_delegation_task(task_id: UUID) -> None:
    try:
        from app.agents.orchestrator import dispatch_persisted_async_delegation

        ok = await dispatch_persisted_async_delegation(task_id.hex)
        if not ok:
            logger.warning("[RuntimeTaskWorker] delegation task {} could not be dispatched", task_id)
    except Exception as exc:  # noqa: BLE001
        _STATE["last_error"] = f"delegation:{type(exc).__name__}:{exc}"
        logger.exception("[RuntimeTaskWorker] delegation task {} failed", task_id)


async def _execute_claimed_subagent_task(task_id: UUID) -> None:
    try:
        from app.services.subagent_run_service import dispatch_persisted_subagent_run

        ok = await dispatch_persisted_subagent_run(task_id.hex)
        if not ok:
            logger.warning("[RuntimeTaskWorker] subagent task {} could not be dispatched", task_id)
    except Exception as exc:  # noqa: BLE001
        _STATE["last_error"] = f"subagent:{type(exc).__name__}:{exc}"
        logger.exception("[RuntimeTaskWorker] subagent task {} failed", task_id)


async def _execute_claimed_trigger_task(task_id: UUID) -> None:
    try:
        from app.services.trigger_daemon import execute_claimed_trigger_runtime_task

        ok = await execute_claimed_trigger_runtime_task(task_id)
        if not ok:
            logger.warning("[RuntimeTaskWorker] trigger task {} could not be dispatched", task_id)
    except Exception as exc:  # noqa: BLE001
        _STATE["last_error"] = f"trigger:{type(exc).__name__}:{exc}"
        logger.exception("[RuntimeTaskWorker] trigger task {} failed", task_id)


async def _execute_claimed_approval_execution_task(task_id: UUID) -> None:
    try:
        from app.services.approval_execution_runtime import execute_claimed_approval_execution

        await execute_claimed_approval_execution(task_id)
    except Exception as exc:  # noqa: BLE001 - preserve uncertainty rather than replaying a side effect.
        _STATE["last_error"] = f"approval_execution:{type(exc).__name__}:{exc}"
        logger.exception("[RuntimeTaskWorker] approval execution task {} failed", task_id)
        try:
            from app.services.runtime_task_service import update_runtime_task_record

            await update_runtime_task_record(
                task_id.hex,
                status="needs_reconciliation",
                result_summary=(
                    "Approval worker failed outside its persisted state machine; side effects require review: "
                    f"{type(exc).__name__}: {exc}"
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
                )
            except Exception as exc:  # convert operational executor failure into the typed terminal contract.
                outcome = TaskExecutionOutcome(
                    status=TaskExecutionStatus.FAILED,
                    summary=f"Business task executor failed: {type(exc).__name__}: {exc}",
                    error_code=type(exc).__name__,
                    retryable=True,
                )
            if not await finalize_business_task_execution(runtime_task_id=runtime_task_id, outcome=outcome):
                raise RuntimeError("business task finalization could not locate the claimed runtime task")
        finally:
            release_business_task_cancel_event(runtime_task_id, cancel_event)
    except Exception as exc:  # noqa: BLE001
        _STATE["last_error"] = f"business_task:{type(exc).__name__}:{exc}"
        logger.exception("[RuntimeTaskWorker] business task {} failed", runtime_task_id)
        try:
            from app.services.runtime_task_service import update_runtime_task_record

            await update_runtime_task_record(
                runtime_task_id.hex,
                status="needs_reconciliation",
                result_summary=(
                    "business_task worker failed outside the atomic finalizer; side effects are unknown: "
                    f"{type(exc).__name__}: {exc}"
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
                await drain_session_event_outbox_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - task claiming must continue after publisher failure.
                _STATE["last_error"] = f"session_event_outbox:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] Session event outbox tick failed")
            try:
                await recover_session_control_inputs_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"session_control_recovery:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] Session control recovery tick failed")
            try:
                await expire_session_permission_requests_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"session_permission_expiry:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] Session permission expiry tick failed")
            try:
                await recover_stale_session_input_admissions_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"input_admission_recovery:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] input admission recovery tick failed")
            try:
                await recover_session_input_dispatches_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"session_input_dispatch:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] Session V2 input dispatch tick failed")
            try:
                await recover_terminal_target_session_inputs_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"session_input_terminal_rollover:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] Session V2 terminal steer rollover tick failed")
            try:
                await recover_turn_replacement_sagas_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"turn_replacement_recovery:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] turn replacement recovery tick failed")
            try:
                await recover_session_model_rounds_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"session_model_round_recovery:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] Session V2 model round recovery tick failed")
            try:
                await recover_session_terminal_outcomes_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"session_terminal_recovery:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] Session V2 terminal recovery tick failed")
            try:
                await reconcile_hr_creation_drafts_once()
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"hr_reconcile:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] HR draft reconciliation tick failed")
            try:
                await reconcile_stale_business_tasks_once()
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"business_task_reconcile:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] BusinessTask reconciliation tick failed")
            try:
                await drain_budget_transition_outbox_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - task claiming must continue after one delivery failure.
                _STATE["last_error"] = f"budget_outbox:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] budget transition outbox tick failed")
            try:
                await drain_channel_delivery_outbox_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - task claiming must continue after one delivery failure.
                _STATE["last_error"] = f"channel_outbox:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] channel delivery outbox tick failed")
            try:
                await drain_runtime_notification_outbox_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - task claiming must continue after one outbox failure.
                _STATE["last_error"] = f"outbox:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] completion outbox tick failed")
            try:
                await recover_team_fanout_admissions_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - normal task claiming must continue.
                _STATE["last_error"] = f"team_fanout_recovery:{type(exc).__name__}: {exc}"
                logger.exception("[RuntimeTaskWorker] Team fanout recovery tick failed")
            try:
                await claim_and_dispatch_once(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - worker loop must survive one bad claim.
                _STATE["last_error"] = f"{type(exc).__name__}: {exc}"
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
