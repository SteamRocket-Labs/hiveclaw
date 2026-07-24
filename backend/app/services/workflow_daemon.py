"""Production workflow resume daemon.

This is intentionally a thin shell: the service owns run semantics, the signal
consumer owns PG Signal matching, and the leaf executor still resolves to the
normal ``spawn_subagent`` path. The daemon just makes those durable mechanisms
run continuously in production instead of existing only as test helpers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import get_settings
from app.runtime.workflow_engine import LeafExecutor
from app.services.workflow_launch import build_resumable_workflow_leaf_executor
from app.services.workflow_runtime_service import WorkflowRuntimeService
from app.services.workflow_signal_consumer import drain_signal_resumes

logger = logging.getLogger(__name__)

_DEFAULT_WORKFLOW_SERVICE = WorkflowRuntimeService()


def get_default_workflow_service() -> WorkflowRuntimeService:
    return _DEFAULT_WORKFLOW_SERVICE


def request_default_workflow_drain() -> None:
    _DEFAULT_WORKFLOW_SERVICE.request_drain()


async def workflow_daemon_tick(
    *,
    service: WorkflowRuntimeService,
    leaf_executor: LeafExecutor,
    session_factory: Any = None,
) -> dict[str, int]:
    resumed = await service.resume_pending_runs(leaf_executor=leaf_executor)
    signal_resumed = await drain_signal_resumes(
        leaf_executor=leaf_executor,
        service=service,
        session_factory=session_factory,
    )
    return {
        "resumed_runs": len(resumed),
        "signal_resumed_runs": len(signal_resumed),
    }


async def start_workflow_daemon(
    *,
    service: WorkflowRuntimeService | None = None,
    leaf_executor: LeafExecutor | None = None,
    interval_seconds: float | None = None,
) -> None:
    from app.services.daemon_liveness import mark_daemon_error, mark_daemon_started, mark_daemon_tick

    runtime_service = service or get_default_workflow_service()
    runtime_service.clear_drain()
    executor = leaf_executor or build_resumable_workflow_leaf_executor()
    interval = interval_seconds
    if interval is None:
        interval = float(getattr(get_settings(), "WORKFLOW_DAEMON_INTERVAL_SECONDS", 15))
    mark_daemon_started("workflow_daemon")

    try:
        while True:
            try:
                result = await workflow_daemon_tick(service=runtime_service, leaf_executor=executor)
            except Exception as exc:
                mark_daemon_error("workflow_daemon", exc)
                logger.exception("[WorkflowDaemon] tick failed")
                await asyncio.sleep(max(interval, 0.1))
                continue
            mark_daemon_tick("workflow_daemon")
            if result["resumed_runs"] or result["signal_resumed_runs"]:
                logger.info("[WorkflowDaemon] tick: %s", result)
            await asyncio.sleep(max(interval, 0.1))
    except asyncio.CancelledError:
        runtime_service.request_drain()
        raise
