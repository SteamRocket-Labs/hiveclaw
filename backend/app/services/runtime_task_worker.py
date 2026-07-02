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
from app.services.runtime_task_claim_service import RuntimeTaskClaimService
from app.services.web_chat_runtime import active_web_chat_run_count, dispatch_web_chat_run, is_executable_chat_task_type


_LOCAL_WAKEUP_EVENT: asyncio.Event | None = None
_STATE: dict[str, Any] = {
    "running": False,
    "worker_id": None,
    "last_claimed_at": None,
    "last_claimed_count": 0,
    "last_error": None,
    "wakeup_sent": 0,
    "wakeup_received": 0,
    "dispatched": 0,
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


def _claim_batch_size_for_available_slots() -> int:
    settings = _settings()
    max_concurrent = max(1, int(settings.RUNTIME_TASK_WORKER_MAX_CONCURRENT))
    configured_batch = max(1, int(settings.RUNTIME_TASK_WORKER_BATCH_SIZE))
    available_slots = max(0, max_concurrent - active_web_chat_run_count())
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
    async with async_session() as db, enter_rls_bypass(db, reason="runtime task worker claim pending executable tasks"):
        service = RuntimeTaskClaimService(
            db=db,
            worker_id=worker_id or _worker_id(),
            task_types=("web_chat_turn", "goal_continuation", "team_member", "advanced_plan"),
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


def _dispatch_claimed_task(task: RuntimeTask) -> bool:
    if is_executable_chat_task_type(getattr(task, "task_type", None)):
        return dispatch_web_chat_run(task.id)
    logger.warning("[RuntimeTaskWorker] Claimed unsupported task type {}; leaving task {}", task.task_type, task.id)
    return False


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
